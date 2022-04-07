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
import time

import aiounittest

from tchap_red_list import ACCOUNT_DATA_TYPE
from tests import SQLiteStore, create_module


class AccountValidityRedListTestCase(aiounittest.AsyncTestCase):
    expired_user = "@expired:example.com"
    valid_user = "@valid:example.com"

    def _setup_account_validity(self, store: SQLiteStore) -> None:
        """Create a table mocking the one created by synapse-email-account-validity,
        except only with the columns used by the red list module, and populate it.

        Args:
            store: the store to use to create and populate the table.
        """
        txn = store.conn.cursor()

        txn.execute(
            """
            CREATE TABLE IF NOT EXISTS email_account_validity(
                user_id TEXT PRIMARY KEY,
                expiration_ts_ms BIGINT NOT NULL
            )
            """,
            (),
        )

        now_ms = int(time.time() * 1000)

        txn.execute(
            "INSERT INTO email_account_validity(user_id, expiration_ts_ms) VALUES(?, ?)",
            (self.expired_user, now_ms - 1000),
        )

        txn.execute(
            "INSERT INTO email_account_validity(user_id, expiration_ts_ms) VALUES(?, ?)",
            (self.valid_user, now_ms + 100000),
        )

        store.conn.commit()

    def _update_expiration_ts(self, store: SQLiteStore, user_id: str, ts: int) -> None:
        """Update the expiration timestamp of a given user.

        Args:
            store: the store to use to perform the update.
            user_id: the user to update the expiration timestamp of.
            ts: the new timestamp.
        """
        txn = store.conn.cursor()

        txn.execute(
            "UPDATE email_account_validity SET expiration_ts_ms = ? WHERE user_id = ?",
            (ts, user_id),
        )

        store.conn.commit()

    async def test_add_expired(self) -> None:
        """Tests that an expired user is correctly added to the red list."""
        module, api, store = await create_module()
        self._setup_account_validity(store)

        # Check that the user is not in the list before we run the update. This also
        # allows us to populate the _get_user_status cache, and check later that it's
        # been correctly invalidated.
        in_list, because_expired = await module._get_user_status(self.expired_user)
        self.assertFalse(in_list)
        self.assertFalse(because_expired)

        # Run the update and check that the expired user got added to the red list (but
        # not the still valid one)
        await module._add_expired_users()

        in_list, because_expired = await module._get_user_status(self.expired_user)
        self.assertTrue(in_list)
        self.assertTrue(because_expired)

        in_list, because_expired = await module._get_user_status(self.valid_user)
        self.assertFalse(in_list)
        self.assertFalse(because_expired)

        # Run the update to remove renewed users and check that nothing has changed.
        await module._remove_renewed_users()

        in_list, because_expired = await module._get_user_status(self.expired_user)
        self.assertTrue(in_list)
        self.assertTrue(because_expired)

        in_list, because_expired = await module._get_user_status(self.valid_user)
        self.assertFalse(in_list)
        self.assertFalse(because_expired)

    async def test_remove_renewed(self) -> None:
        """Tests that a user that was added to the red list after expiring but renewed
        their account since is correctly removed from it."""
        module, api, store = await create_module()
        self._setup_account_validity(store)

        await module._add_expired_users()

        in_list, because_expired = await module._get_user_status(self.expired_user)
        self.assertTrue(in_list)
        self.assertTrue(because_expired)

        now_ts = int(time.time() * 1000)
        self._update_expiration_ts(store, self.expired_user, now_ts + 100)

        await module._remove_renewed_users()

        in_list, because_expired = await module._get_user_status(self.expired_user)
        self.assertFalse(in_list)
        self.assertFalse(because_expired)

    async def test_update_after_renewed(self) -> None:
        """Tests that if a user adds themselves to the red list manually before the
        update that automatically removes the renewed users, they don't get removed.
        """
        module, api, store = await create_module()
        self._setup_account_validity(store)

        # Add the user to the red list automatically.
        await module._add_expired_users()

        in_list, because_expired = await module._get_user_status(self.expired_user)
        self.assertTrue(in_list)
        self.assertTrue(because_expired)

        # Renew the user. We can do this anywhere as long as it's before the call to
        # _remove_renewed_users, but doing this here is more natural as in real life
        # users aren't permitted to update their account data if they're expired.
        now_ts = int(time.time() * 1000)
        self._update_expiration_ts(store, self.expired_user, now_ts + 100)

        # Manually hide the user's profile.
        await module.update_red_list_status(
            user_id=self.expired_user,
            room_id=None,
            account_data_type=ACCOUNT_DATA_TYPE,
            content={"hide_profile": True},
        )

        # Check that the user is still in the list but its because_expired flag has
        # changed.
        in_list, because_expired = await module._get_user_status(self.expired_user)
        self.assertTrue(in_list)
        self.assertFalse(because_expired)

        # Run the update and check that the user is still there.
        await module._remove_renewed_users()

        in_list, because_expired = await module._get_user_status(self.expired_user)
        self.assertTrue(in_list)
        self.assertFalse(because_expired)
