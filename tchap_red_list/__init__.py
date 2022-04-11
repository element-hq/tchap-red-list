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
from typing import Any, Dict, Optional, Tuple

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

logger = logging.getLogger(__name__)

ACCOUNT_DATA_TYPE = "im.vector.hide_profile"


@attr.s(auto_attribs=True, frozen=True)
class RedListManagerConfig:
    discovery_room: Optional[str] = None


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
        desired_status = content.get("hide_profile") or False
        current_status, _ = await self._get_user_status(user_id)

        if current_status == desired_status:
            return

        # Add or remove the user depending on whether they want their profile hidden.
        if desired_status is True:
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

    async def _setup_db(self) -> None:
        """Create the table needed to store the red list data."""

        def setup_db_txn(txn: LoggingTransaction) -> None:
            sql = """
            CREATE TABLE IF NOT EXISTS tchap_red_list(
                user_id TEXT PRIMARY KEY,
                because_expired BOOLEAN NOT NULL DEFAULT FALSE
            );
            """
            txn.execute(sql, ())

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
            because_expired: whether the user is being added as a result of a change in
                their account validity status.
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

            return True, row["because_expired"]

        return await self._api.run_db_interaction(
            "tchap_red_list_get_status",
            _get_user_status_txn,
        )
