# Copyright 2017 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# Licensed under the Apache License, Version 2.0
# Slack Bot DM client — (c) Paul Seifer, Autobrains LTD

from __future__ import annotations

try:
    from slack_sdk import WebClient
    from slack_sdk.errors import SlackApiError
    _SDK_AVAILABLE = True
except ImportError:
    _SDK_AVAILABLE = False


FALLBACK_EMPLOYEE = "Paul Seifer"


class SlackDMClient:
    """Send Slack DMs to employees by name or email via a Bot OAuth token.

    Employee lookup order:
      1. If value contains '@' → users.lookupByEmail (exact, fast)
      2. Otherwise → match against real_name / display_name from users.list
         (fetched once at first use, then cached for the process lifetime)
    """

    def __init__(self, bot_token: str) -> None:
        if not _SDK_AVAILABLE:
            raise ImportError("slack-sdk package is not installed")
        self._client = WebClient(token=bot_token)
        self._user_id_cache: dict[str, str] = {}    # employee string → user_id
        self._channel_cache: dict[str, str] = {}    # user_id → DM channel_id
        self._users_by_name: dict[str, str] = {}    # lowercase name → user_id
        self._users_by_email: dict[str, str] = {}   # lowercase email → user_id
        self._users_list_loaded = False

    # ── User resolution ───────────────────────────────────────────────────────

    def _load_users_list(self) -> None:
        if self._users_list_loaded:
            return
        cursor = None
        try:
            while True:
                kwargs: dict = {"limit": 200}
                if cursor:
                    kwargs["cursor"] = cursor
                result = self._client.users_list(**kwargs)
                for member in result.get("members", []):
                    if member.get("deleted") or member.get("is_bot"):
                        continue
                    uid = member["id"]
                    profile = member.get("profile", {})
                    for field in ("real_name", "display_name"):
                        name = (profile.get(field) or "").strip().lower()
                        if name:
                            self._users_by_name[name] = uid
                    email = (profile.get("email") or "").strip().lower()
                    if email:
                        self._users_by_email[email] = uid
                cursor = (result.get("response_metadata") or {}).get("next_cursor")
                if not cursor:
                    break
            self._users_list_loaded = True
            print(f"slack_dm: loaded {len(self._users_by_name)} users from workspace")
        except SlackApiError as exc:
            print(f"slack_dm: failed to load users list: {exc}")

    def find_user_id(self, employee: str) -> str | None:
        if employee in self._user_id_cache:
            return self._user_id_cache[employee]

        uid: str | None = None

        if "@" in employee:
            try:
                result = self._client.users_lookupByEmail(email=employee)
                uid = result["user"]["id"]
            except SlackApiError as exc:
                print(f"slack_dm: lookupByEmail('{employee}') failed: {exc.response['error']}")

        if uid is None:
            self._load_users_list()
            key = employee.strip().lower()
            uid = self._users_by_name.get(key) or self._users_by_email.get(key)

        if uid:
            self._user_id_cache[employee] = uid
            print(f"slack_dm: resolved '{employee}' → {uid}")
        else:
            print(f"slack_dm: could not resolve '{employee}' to a Slack user")

        return uid

    # ── DM channel ────────────────────────────────────────────────────────────

    def _open_dm_channel(self, user_id: str) -> str | None:
        if user_id in self._channel_cache:
            return self._channel_cache[user_id]
        try:
            result = self._client.conversations_open(users=[user_id])
            channel_id: str = result["channel"]["id"]
            self._channel_cache[user_id] = channel_id
            return channel_id
        except SlackApiError as exc:
            print(f"slack_dm: conversations_open({user_id}) failed: {exc.response['error']}")
            return None

    # ── Public API ────────────────────────────────────────────────────────────

    def send_dm(self, employee: str, message: str) -> bool:
        """Send a DM to employee. Returns True on success.

        If employee cannot be resolved to a Slack user, falls back to
        FALLBACK_EMPLOYEE so the alert is not silently dropped.
        """
        try:
            recipient = employee
            user_id = self.find_user_id(employee)
            if not user_id and employee != FALLBACK_EMPLOYEE:
                print(f"slack_dm: '{employee}' unresolvable — falling back to '{FALLBACK_EMPLOYEE}'")
                recipient = FALLBACK_EMPLOYEE
                user_id = self.find_user_id(FALLBACK_EMPLOYEE)
            if not user_id:
                return False
            channel_id = self._open_dm_channel(user_id)
            if not channel_id:
                return False
            self._client.chat_postMessage(channel=channel_id, text=message)
            print(f"slack_dm: DM sent to '{recipient}'")
            return True
        except SlackApiError as exc:
            print(f"slack_dm: send_dm('{employee}') failed: {exc.response['error']}")
            return False
