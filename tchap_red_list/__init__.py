# Copyright 2022 New Vector Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
import logging
import time
from typing import Any, Dict, List, Optional, Tuple

import attr
from synapse.module_api import (
    DatabasePool,
    JsonDict,
    LoggingTransaction,
    ModuleApi,
    UserProfile,
    cached,
    run_in_background,
)
from synapse.module_api.errors import ConfigError, SynapseError

logger = logging.getLogger(__name__)

ACCOUNT_DATA_TYPE = "im.vector.hide_profile"


@attr.s(auto_attribs=True, frozen=True)
class RedListManagerConfig:
    discovery_room: Optional[str] = None
    use_email_account_validity: bool = False


class RedListManager:
    def __init__(
        self, config: RedListManagerConfig, api: ModuleApi, setup_db: bool = True
    ):
        # Keep a reference to the config and Module API
        self._api = api
        self._config = config

        # Register callbacks
        self._api.register_account_data_callbacks(
            on_account_data_updated=self.update_red_list_status,
        )

        self._api.register_spam_checker_callbacks(
            check_username_for_spam=self.check_user_in_red_list,
        )

        if setup_db:
            # Set up the storage layer
            # We run this in the background because there's no other way to run async code
            # in __init__. However, this means we might have a race if something causes
            # the table to be accessed before it's fully created.
            run_in_background(self._setup_db)

        if self._config.use_email_account_validity:
            self._api.looping_background_call(self._add_expired_users, 60 * 60 * 1000)
            self._api.looping_background_call(
                self._remove_renewed_users, 60 * 60 * 1000
            )

    @staticmethod
    def parse_config(config: Dict[str, Any]) -> RedListManagerConfig:
        return RedListManagerConfig(**config)

    async def update_red_list_status(
        self,
        user_id: str,
        room_id: Optional[str],
        account_data_type: str,
        content: JsonDict,
    ) -> None:
        """Update a user's status in the red list when their account data changes.
        Implements the on_account_data_updated account data callback.
        """
        if account_data_type != ACCOUNT_DATA_TYPE:
            return

        # Compare what status (in the list, not in the list) the user wants to have with
        # what it already has. If they're the same, don't do anything more.
        desired_status = bool(content.get("hide_profile"))
        current_status, because_expired = await self._get_user_status(user_id)

        # If the user's desired status is the same as their current one, don't do
        # anything, except if because_expired is True (because in this case it can be a
        # user that was added to the red list after expiring, haven't been removed yet,
        # and manually re-added themselves).
        if current_status == desired_status and because_expired is False:
            return

        # Update the red list depending on whether the user wants their profile hidden.
        if because_expired is True:
            await self._update_added_after_expiring(user_id)
        elif desired_status is True:
            await self._add_to_red_list(user_id)
        else:
            await self._remove_from_red_list(user_id)

    async def _maybe_change_membership_in_discovery_room(
        self, user_id: str, membership: str
    ) -> None:
        """Change a user's membership in the discovery room.

        Does nothing if no discover room has been configured.

        Args:
            user_id: the user to change the membership of.
            membership: the membership to set for this user.
        """
        if self._config.discovery_room is None:
            return

        await self._api.update_room_membership(
            sender=user_id,
            target=user_id,
            room_id=self._config.discovery_room,
            new_membership=membership,
        )

    async def check_user_in_red_list(self, user_profile: UserProfile) -> bool:
        """Check if a user should be in the red list, which means they need to be hidden
        from local user directory search results.
        Implements the check_username_for_spam spam checker callback.
        """
        user_in_red_list, _ = await self._get_user_status(user_profile["user_id"])
        return user_in_red_list

    async def _add_expired_users(self) -> None:
        """Retrieve all expired users and adds them to the red list."""

        def add_expired_users_txn(txn: LoggingTransaction) -> List[str]:
            # Retrieve all the expired users.
            sql = """
            SELECT user_id FROM email_account_validity WHERE expiration_ts_ms <= ?
            """

            now_ms = int(time.time() * 1000)
            txn.execute(sql, (now_ms,))
            expired_users_rows = txn.fetchall()

            expired_users = [row[0] for row in expired_users_rows]

            # Figure out which users are in the red list.
            # We could also inspect the cache on self._get_user_status and only query the
            # status of the users that aren't cached, but
            #   1) it's probably digging too much into Synapse's internals (i.e. it could
            #      easily break without warning)
            #   2) it's not clear that there would be such a huge perf gain from doing
            #      things this way.
            red_list_users_rows = DatabasePool.simple_select_many_txn(
                txn=txn,
                table="tchap_red_list",
                column="user_id",
                iterable=expired_users,
                keyvalues={},
                retcols=["user_id"],
            )

            # Figure out which users we need to add to the red list by looking up whether
            # they're already in it.
            users_in_red_list = [row["user_id"] for row in red_list_users_rows]
            users_to_add = [
                user for user in expired_users if user not in users_in_red_list
            ]

            # Add all the expired users not in the red list.
            sql = """
            INSERT INTO tchap_red_list(user_id, because_expired) VALUES(?, ?)
            """
            for user in users_to_add:
                txn.execute(sql, (user, True))
                self._get_user_status.invalidate((user,))

            return users_to_add

        users_added = await self._api.run_db_interaction(
            "tchap_red_list_hide_expired_users",
            add_expired_users_txn,
        )

        # Make the expired users leave the discovery room if there's one.
        for user in users_added:
            await self._maybe_change_membership_in_discovery_room(user, "leave")

    async def _remove_renewed_users(self) -> None:
        """Remove users from the red list if they have been added by _add_expired_users
        and have since then renewed their account.
        """

        def remove_renewed_users_txn(txn: LoggingTransaction) -> List[str]:
            # Retrieve the list of users we have previously added because their account
            # expired.
            rows = DatabasePool.simple_select_list_txn(
                txn=txn,
                table="tchap_red_list",
                keyvalues={"because_expired": True},
                retcols=["user_id"],
            )

            previously_expired_users = [row["user_id"] for row in rows]

            # Among these users, figure out which ones are still expired.
            rows = DatabasePool.simple_select_many_txn(
                txn=txn,
                table="email_account_validity",
                column="user_id",
                iterable=previously_expired_users,
                keyvalues={},
                retcols=["user_id", "expiration_ts_ms"],
            )

            renewed_users: List[str] = []
            now_ms = int(time.time() * 1000)
            for row in rows:
                if row["expiration_ts_ms"] > now_ms:
                    renewed_users.append(row["user_id"])

            # Remove the users who aren't expired anymore.
            DatabasePool.simple_delete_many_txn(
                txn=txn,
                table="tchap_red_list",
                column="user_id",
                values=renewed_users,
                keyvalues={},
            )

            for user in renewed_users:
                self._get_user_status.invalidate((user,))

            return renewed_users

        users_removed = await self._api.run_db_interaction(
            "tchap_red_list_remove_renewed_users",
            remove_renewed_users_txn,
        )

        # Make the renewed users re-join the discovery room if there's one.
        for user in users_removed:
            await self._maybe_change_membership_in_discovery_room(user, "join")

    async def _setup_db(self) -> None:
        """Create the table needed to store the red list data.

        If the module is configured to interact with the email account validity module,
        also check that the table exists.
        """

        def setup_db_txn(txn: LoggingTransaction) -> None:
            sql = """
            CREATE TABLE IF NOT EXISTS tchap_red_list(
                user_id TEXT PRIMARY KEY,
                because_expired BOOLEAN NOT NULL DEFAULT FALSE
            );
            """
            txn.execute(sql, ())

            if self._config.use_email_account_validity:
                try:
                    txn.execute("SELECT * FROM email_account_validity LIMIT 0", ())
                except SynapseError:
                    raise ConfigError(
                        "use_email_account_validity is set but no email account validity"
                        " database table found."
                    )

        await self._api.run_db_interaction(
            "tchap_red_list_setup_db",
            setup_db_txn,
        )

    async def _add_to_red_list(
        self,
        user_id: str,
        because_expired: bool = False,
    ) -> None:
        """Add the given user to the red list.

        Args:
            user_id: the user to add to the red list.
            because_expired: whether the user is being added as a result of their
                account expiring.
        """

        def _add_to_red_list_txn(txn: LoggingTransaction) -> None:
            sql = """
            INSERT INTO tchap_red_list(user_id, because_expired) VALUES (?, ?)
            """
            txn.execute(sql, (user_id, because_expired))

            self._get_user_status.invalidate((user_id,))

        await self._api.run_db_interaction(
            "tchap_red_list_add",
            _add_to_red_list_txn,
        )

        # If there is a room used for user discovery, make them leave it.
        await self._maybe_change_membership_in_discovery_room(user_id, "leave")

    async def _update_added_after_expiring(self, user_id: str) -> None:
        """Update a user's entry into the red list to set the because_expired boolean to
        false.

        We need this because there can be a delay between the user renewing their account
        (from an account validity perspective) and the module actually picking up the
        renewal, during which the user might decide to add their profile to the red list.

        In this case, we set because_expired to false for the user, which will cause the
        job that removes unexpired accounts from the red list to ignore them.

        Args:
            user_id: the user to update.
        """

        def update_added_after_expiring_txn(txn: LoggingTransaction) -> None:
            DatabasePool.simple_update_one_txn(
                txn=txn,
                table="tchap_red_list",
                keyvalues={"user_id": user_id},
                updatevalues={"because_expired": False},
            )

            self._get_user_status.invalidate((user_id,))

        await self._api.run_db_interaction(
            "tchap_red_list_update_added_after_expiring",
            update_added_after_expiring_txn,
        )

    async def _remove_from_red_list(self, user_id: str) -> None:
        """Remove the given user from the red list.

        Args:
            user_id: the user to remove from the red list.
        """

        def _remove_from_red_list_txn(txn: LoggingTransaction) -> None:
            sql = """
            DELETE FROM tchap_red_list WHERE user_id = ?
            """
            txn.execute(sql, (user_id,))

            self._get_user_status.invalidate((user_id,))

        await self._api.run_db_interaction(
            "tchap_red_list_remove",
            _remove_from_red_list_txn,
        )

        # If there is a room used for user discovery, make them join it.
        await self._maybe_change_membership_in_discovery_room(user_id, "join")

    @cached()
    async def _get_user_status(self, user_id: str) -> Tuple[bool, bool]:
        """Whether the given user is in the red list, and if so whether they have been
        added as a result of their account expiring.

        Args:
            user_id: the user to check.

        Returns:
            A tuple with the following values:
                * a boolean indicating whether the user is in the red list
                * a boolean indicating whether the user was added to the red list as a
                  result of their account expiring. Always False if the first value of
                  the tuple is False.
        """

        def _get_user_status_txn(txn: LoggingTransaction) -> Tuple[bool, bool]:
            row = DatabasePool.simple_select_one_txn(
                txn=txn,
                table="tchap_red_list",
                keyvalues={"user_id": user_id},
                retcols=["because_expired"],
                allow_none=True,
            )

            if row is None:
                return False, False

            return True, bool(row["because_expired"])

        return await self._api.run_db_interaction(
            "tchap_red_list_get_status",
            _get_user_status_txn,
        )
