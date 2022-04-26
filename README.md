# "Red list" module for Tchap

This module allows users to hide themselves from user search.

Users are expected to be in a single room, hidden from clients, to help user discovery
across a closed federation. When users update their global `im.vector.hide_profile`
account data with `{"hide_profile": True}`, they are removed from this discovery room,
and added to a local database table to filter them out from local results.

This module can also interact with the [synapse-email-account-validity](https://github.com/matrix-org/synapse-email-account-validity)
module. If this compatibility feature is enabled, the module will automatically scan for
expired and renewed users every hour. It will then add expired users to the red list and
remove renewed users from it (without updating the users' account data).

## Installation

From the virtual environment that you use for Synapse, install this module with:
```shell
pip install path/to/tchap-red-list
```
(If you run into issues, you may need to upgrade `pip` first, e.g. by running
`pip install --upgrade pip`)

Then alter your homeserver configuration, adding to your `modules` configuration:
```yaml
modules:
  - module: tchap_red_list.RedListManager
    config:
      # ID of the room used for user discovery.
      # Optional, defaults to no room.
      discovery_room: "!someroom:example.com"
      # Whether to enable compatibility with the synapse-email-account-validity module.
      # Optional, defaults to false.
      use_email_account_validity: false
```


## Development

In a virtual environment with pip ≥ 21.1, run
```shell
pip install -e .[dev]
```

To run the unit tests, you can either use:
```shell
tox -e py
```
or
```shell
trial tests
```

To run the linters and `mypy` type checker, use `./scripts-dev/lint.sh`.


## Releasing

The exact steps for releasing will vary; but this is an approach taken by the
Synapse developers (assuming a Unix-like shell):

 1. Set a shell variable to the version you are releasing (this just makes
    subsequent steps easier):
    ```shell
    version=X.Y.Z
    ```

 2. Update `setup.cfg` so that the `version` is correct.

 3. Stage the changed files and commit.
    ```shell
    git add -u
    git commit -m v$version -n
    ```

 4. Push your changes.
    ```shell
    git push
    ```

 5. When ready, create a signed tag for the release:
    ```shell
    git tag -s v$version
    ```
    Base the tag message on the changelog.

 6. Push the tag.
    ```shell
    git push origin tag v$version
    ```