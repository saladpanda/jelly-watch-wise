import json

import requests

from config import logger
from jellyfin.stats import AggregatedStatsSource

TICKS_PER_SEC = 10_000_000


class ServerApi:
    def __init__(self, server, token, stats: AggregatedStatsSource):
        self.server = server
        self.token = token
        self.headers = {'Authorization': f'MediaBrowser Token={self.token}',
                        'Accept': 'application/json',
                        'Content-Type': 'application/json'}
        self.decoder = json.JSONDecoder()
        self.stats = stats

    def get_users(self):
        users = {}
        r = requests.get(f'{self.server}/Users', headers=self.headers)
        if r.status_code == 200:
            users = self.decoder.decode(r.text)
            users = {x["Id"]: x["Name"] for x in users}
        return users

    def get_playing_sessions(self, user_id):
        """Sessions of the user that currently play something (with runtime/position info)."""
        r = requests.get(f'{self.server}/Sessions', headers=self.headers, timeout=10)
        if r.status_code != 200:
            logger.warning(f'could not fetch sessions, HTTP {r.status_code}')
            return []
        result = []
        for s in self.decoder.decode(r.text):
            if s.get('UserId') != user_id or 'NowPlayingItem' not in s:
                continue
            item = s['NowPlayingItem']
            play_state = s.get('PlayState') or {}
            runtime = item.get('RunTimeTicks')
            position = play_state.get('PositionTicks')
            remaining_sec = None
            if runtime is not None and position is not None:
                remaining_sec = max(0.0, (runtime - position) / TICKS_PER_SEC)
            result.append({
                'id': s['Id'],
                'device': s.get('DeviceName', '?'),
                'client': s.get('Client', '?'),
                'item_id': item.get('Id'),
                'item_name': item.get('Name', '?'),
                'remaining_sec': remaining_sec,
                'paused': bool(play_state.get('IsPaused', False)),
                'remote_control': bool(s.get('SupportsRemoteControl', False)),
            })
        return result

    def get_playing_session_ids(self, user_id):
        """Ids of sessions of the user that currently play something."""
        return [s['id'] for s in self.get_playing_sessions(user_id)]

    def stop_session(self, session_id):
        """Sends the Stop playstate command to the given session."""
        r = requests.post(f'{self.server}/Sessions/{session_id}/Playing/Stop', headers=self.headers, timeout=10)
        ok = r.status_code in (200, 204)
        if ok:
            logger.info(f'stop command accepted for session {session_id}')
        else:
            logger.warning(f'stop command for session {session_id} failed, HTTP {r.status_code}: {r.text[:200]}')
        return ok

    def send_message(self, session_id, header, text, timeout_ms):
        """Shows a toast message on the media player of the given session."""
        body = {'Header': header, 'Text': text, 'TimeoutMs': timeout_ms}
        r = requests.post(f'{self.server}/Sessions/{session_id}/Message', headers=self.headers,
                          data=json.dumps(body), timeout=10)
        return r.status_code in (200, 204)

    def get_total_time_sec(self, user_id, date_start, date_end):
        return self.stats.get_total_time_sec(user_id, date_start, date_end)

    def get_user_policy(self, user_id):
        user = requests.get(f'{self.server}/Users/{user_id}', headers=self.headers)
        if user.status_code != 200:
            print("Error fetching user data")
            return None
        return self.decoder.decode(user.text)["Policy"]

    def set_user_policy(self, user_id, policy):
        r = requests.post(f'{self.server}/Users/{user_id}/Policy', headers=self.headers, data=json.dumps(policy))
        if r.status_code != 204:
            print("Error on updating user policy")

    def disable_user(self, user_id, is_disabled: bool):
        policy = self.get_user_policy(user_id)
        policy["IsDisabled"] = is_disabled
        self.set_user_policy(user_id, policy)

    def is_user_disabled(self, user_id):
        policy = self.get_user_policy(user_id)
        return policy["IsDisabled"]

    def get_enabled_folders(self, user_id):
        return self.get_user_policy(user_id)["EnabledFolders"]

    def set_enabled_folders(self, user_id, folders):
        policy = self.get_user_policy(user_id)
        policy["EnabledFolders"] = folders
        self.set_user_policy(user_id, policy)
