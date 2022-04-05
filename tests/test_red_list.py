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
from typing import Optional, Tuple
from unittest.mock import Mock

import aiounittest
from synapse.module_api import JsonDict

from tchap_red_list import ACCOUNT_DATA_TYPE, RedListManager
from tests import create_module


class RedListTestCase(aiounittest.AsyncTestCase):
    user_id = "@alice:example"

    async def test_other_data_type(self) -> None:
        """Tests that incoming account data with a different account data type than the
        one the module handles is ignored.
        """
        module, api, _ = await create_module()

        account_data_type = "org.matrix.foo"

        await module.update_red_list_status(
            user_id=self.user_id,
            room_id=None,
            account_data_type=account_data_type,
            content={"foo": "bar"},
        )

        api.run_db_interaction.assert_not_called()
        api.update_room_membership.assert_not_called()

    async def test_no_hide_profile(self) -> None:
        """Tests that account data of the right type but with a content that doesn't
        include the hide_profile property is considered as if it was present and equal to
        False.
        """
        module, api = await self._setup_user_in_list()

        # Invalidate the cache, so it doesn't interfere with the call counts.
        module._get_user_status.invalidate((self.user_id,))

        # Trigger the callback with an account data that's missing the hide_profile key
        # in its content.
        await module.update_red_list_status(
            user_id=self.user_id,
            room_id=None,
            account_data_type=ACCOUNT_DATA_TYPE,
            content={},
        )

        # There should be two database calls: one to check the user's status, and one to
        # update it.
        self.assertEqual(
            api.run_db_interaction.call_count, 2, api.run_db_interaction.mock_calls
        )
        api.update_room_membership.assert_not_called()

        # Check that the user is not in the list anymore.
        in_list, _ = await module._get_user_status(self.user_id)
        self.assertFalse(in_list)

    async def test_status_cached(self) -> None:
        """Tests that users statuses are correctly cached and invalidated."""
        module, api, _ = await create_module()

        # Get the user's status and check we made a database call.
        in_list, _ = await module._get_user_status(self.user_id)
        self.assertFalse(in_list)
        api.run_db_interaction.assert_called_once()

        # Get the user's status again and check we didn't make an additional database
        # call.
        in_list, _ = await module._get_user_status(self.user_id)
        self.assertFalse(in_list)
        api.run_db_interaction.assert_called_once()

        # Add the user to the list and get their status again to check that it
        # invalidated the cache (and caused another database call).
        await module.update_red_list_status(
            user_id=self.user_id,
            room_id=None,
            account_data_type=ACCOUNT_DATA_TYPE,
            content={"hide_profile": True},
        )
        # Reset the mock so that the database call from update_red_list_status doesn't
        # interfere.
        api.run_db_interaction.reset_mock()
        in_list, _ = await module._get_user_status(self.user_id)
        self.assertTrue(in_list)
        api.run_db_interaction.assert_called_once()

        # Remove the user from the list and check the same thing.
        await module.update_red_list_status(
            user_id=self.user_id,
            room_id=None,
            account_data_type=ACCOUNT_DATA_TYPE,
            content={"hide_profile": False},
        )
        # Reset the mock so that the database call from update_red_list_status doesn't
        # interfere.
        api.run_db_interaction.reset_mock()
        in_list, _ = await module._get_user_status(self.user_id)
        self.assertFalse(in_list)
        api.run_db_interaction.assert_called_once()

    async def test_add_to_list_no_discovery(self) -> None:
        """Tests adding a user to the red list (with no discovery room)"""
        module, api, _ = await create_module()

        await module.update_red_list_status(
            user_id=self.user_id,
            room_id=None,
            account_data_type=ACCOUNT_DATA_TYPE,
            content={"hide_profile": True},
        )

        self.assertEqual(
            api.run_db_interaction.call_count, 2, api.run_db_interaction.mock_calls
        )
        api.update_room_membership.assert_not_called()

        in_list, _ = await module._get_user_status(self.user_id)

        self.assertTrue(in_list)

    async def test_remove_from_list_no_discovery(self) -> None:
        """Tests removing a user from the red list (with no discovery room)"""
        module, api = await self._setup_user_in_list()

        await module.update_red_list_status(
            user_id=self.user_id,
            room_id=None,
            account_data_type=ACCOUNT_DATA_TYPE,
            content={"hide_profile": False},
        )

        self.assertEqual(
            api.run_db_interaction.call_count, 2, api.run_db_interaction.mock_calls
        )
        api.update_room_membership.assert_not_called()

        in_list, _ = await module._get_user_status(self.user_id)

        self.assertFalse(in_list)

    async def test_add_to_list_discovery(self) -> None:
        """Tests adding a user to the red list (with a discovery room)"""
        room_id = "!someroom:test"
        module, api, _ = await create_module({"discovery_room": room_id})

        await module.update_red_list_status(
            user_id=self.user_id,
            room_id=None,
            account_data_type=ACCOUNT_DATA_TYPE,
            content={"hide_profile": True},
        )

        self.assertEqual(
            api.run_db_interaction.call_count, 2, api.run_db_interaction.mock_calls
        )
        api.update_room_membership.assert_called_once_with(
            sender=self.user_id,
            target=self.user_id,
            room_id=room_id,
            new_membership="leave",
        )

        in_list, _ = await module._get_user_status(self.user_id)

        self.assertTrue(in_list)

    async def test_remove_from_list_discovery(self) -> None:
        """Tests removing a user from the red list (with a discovery room)"""
        room_id = "!someroom:test"
        module, api = await self._setup_user_in_list({"discovery_room": room_id})

        await module.update_red_list_status(
            user_id=self.user_id,
            room_id=None,
            account_data_type=ACCOUNT_DATA_TYPE,
            content={"hide_profile": False},
        )

        self.assertEqual(
            api.run_db_interaction.call_count, 2, api.run_db_interaction.mock_calls
        )
        api.update_room_membership.assert_called_once_with(
            sender=self.user_id,
            target=self.user_id,
            room_id=room_id,
            new_membership="join",
        )

        in_list, _ = await module._get_user_status(self.user_id)

        self.assertFalse(in_list)

    async def _setup_user_in_list(
        self, config: Optional[JsonDict] = None
    ) -> Tuple[RedListManager, Mock]:
        """Performs the initial setup for tests that require a user to already be present
        in the red list.

        Args:
            config: a config to pass onto create_module.

        Returns:
            The return values from create_module.
        """
        module, api, _ = await create_module(config)
        await module._add_to_red_list(self.user_id)
        # Reset the mocks, so the action we just performed doesn't interfere with the
        # call counts.
        api.run_db_interaction.reset_mock()
        api.update_room_membership.reset_mock()
        return module, api
