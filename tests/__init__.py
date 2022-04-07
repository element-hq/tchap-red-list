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
import sqlite3
from asyncio import Future
from typing import Any, Awaitable, Callable, Optional, Tuple, TypeVar
from unittest.mock import Mock

from synapse.module_api import JsonDict, ModuleApi

from tchap_red_list import RedListManager

RV = TypeVar("RV")
TV = TypeVar("TV")


class SQLiteStore:
    """In-memory SQLite store. We can't just use a run_db_interaction function that opens
    its own connection, since we need to use the same connection for all queries in a
    test.
    """

    def __init__(self) -> None:
        self.conn = sqlite3.connect(":memory:")

    async def run_db_interaction(
        self, desc: str, f: Callable[..., RV], *args: Any, **kwargs: Any
    ) -> RV:
        cur = self.conn.cursor()
        try:
            res = f(cur, *args, **kwargs)
            self.conn.commit()
            return res
        except Exception:
            self.conn.rollback()
            raise


def make_awaitable(result: TV) -> Awaitable[TV]:
    """
    Makes an awaitable, suitable for mocking an `async` function.
    This uses Futures as they can be awaited multiple times so can be returned
    to multiple callers.
    """
    future = Future()  # type: ignore
    future.set_result(result)
    return future


async def create_module(
    config: Optional[JsonDict] = None,
) -> Tuple[RedListManager, Mock]:
    store = SQLiteStore()

    # Create a mock based on the ModuleApi spec, but override some mocked functions
    # because some capabilities are needed for running the tests.
    module_api = Mock(spec=ModuleApi)
    module_api.run_db_interaction.side_effect = store.run_db_interaction
    module_api.update_room_membership.return_value = make_awaitable(None)

    # If necessary, give parse_config some configuration to parse.
    raw_config = config if config is not None else {}
    parsed_config = RedListManager.parse_config(raw_config)

    # Set up the database table in a separate step, to ensure it's ready to be used when
    # we run the tests.
    module = RedListManager(parsed_config, module_api, setup_db=False)
    await module._setup_db()

    # Instantiating the module will set up the database, which will call
    # run_db_interaction. We don't want to count this call in tests, so we reset its
    # call history.
    module_api.run_db_interaction.reset_mock()

    return module, module_api
