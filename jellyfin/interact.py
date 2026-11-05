import os.path
import threading
import time as time_module
from datetime import datetime, timedelta

import i18n
import yaml

from config import logger
from jellyfin.api import ServerApi
from jellyfin.stats import PlaytimeReporting, JellyStats
from misc import clip


class FoldersBackup:
    def __init__(self):
        self.folder_backup_name = 'config/user-folders.bck'

    def keep_user_folders(self, user_id, folders):
        count = len(folders)
        logger.debug(f'keep folders of user {user_id}, total {count}')
        if count > 0:
            folders_collection = {}
            if os.path.isfile(self.folder_backup_name):
                with open(self.folder_backup_name, 'r') as file:
                    folders_collection = yaml.safe_load(file)
            folders_collection[user_id] = folders
            with open(self.folder_backup_name, 'w') as file:
                yaml.dump(folders_collection, file)

    def restore_user_folders(self, user_id):
        logger.debug(f'restore user folders {user_id}')
        if os.path.isfile(self.folder_backup_name):
            with open(self.folder_backup_name, 'r') as file:
                backup = yaml.safe_load(file)
                if user_id in backup:
                    return backup[user_id]
        return []


STOP_LOOP_SEC = 60


class ServerInteraction:
    def __init__(self, config):
        self.config = config
        self.backup = FoldersBackup()
        if config.stats_host:
            stats = JellyStats(config.stats_host, config.stats_token)
        else:
            stats = PlaytimeReporting(config.host, config.token)
        self.api = ServerApi(config.host, config.token, stats)
        self.select_users = config.get_select_users(self.api.get_users())
        self.user_data = self.get_user_data()
        self.notified = {}  # user_id -> set of 'minutes left' thresholds already announced
        self.stopping = set()  # session ids with a running stop loop
        self.grace_logged = set()  # (session_id, item_id) pairs for which the grace decision was logged

    def get_user_data(self):
        user_data = {}
        for user_id in self.select_users:
            limit = self.config.get_limit(user_id)
            folders = self.api.get_enabled_folders(user_id)
            if not self.are_only_unlimited_folders(folders):
                self.backup.keep_user_folders(user_id, folders)
            user_data[user_id] = {'folders': folders, 'altered_limit': limit}
        return user_data

    def are_only_unlimited_folders(self, folders):
        return len(folders) == 0 or all(x in self.config.no_limit_folders for x in folders)

    def keep_unlimited_folders(self, folders):
        return [x for x in folders if x in self.config.no_limit_folders]

    def media_folders_locker(self, user_id):
        logger.debug('media folders lock/unlock')
        time = self.get_today_watched_min(user_id)
        time_left = self.user_data[user_id]['altered_limit'] - time
        self.notify_time_left(user_id, time_left)
        folders = self.api.get_enabled_folders(user_id)
        if not self.are_only_unlimited_folders(folders):
            self.backup.keep_user_folders(user_id, folders)
        if time_left > 0:
            prev_folders = self.user_data[user_id]['folders']
            if self.are_only_unlimited_folders(prev_folders):
                prev_folders = self.backup.restore_user_folders(user_id)
            if len(prev_folders) > 0 and len(prev_folders) > len(folders):
                self.api.set_enabled_folders(user_id, prev_folders)
                logger.info('folders restored')
        else:
            if not self.are_only_unlimited_folders(folders):
                self.user_data[user_id]['folders'] = folders  # store all folders for later restore
                kept_folders = self.keep_unlimited_folders(folders)
                self.api.set_enabled_folders(user_id, kept_folders)
                logger.info('folders disabled - soft lock action')
            if self.config.stop_active_sessions:
                self.stop_playback(user_id, time_left)

    def stop_playback(self, user_id, time_left):
        """Stops running playback of a user whose time is up (unless the item is about to end anyway)."""
        user_name = self.select_users.get(user_id, user_id)
        try:
            sessions = self.api.get_playing_sessions(user_id)
        except Exception as e:
            logger.warning(f'stop check for user {user_name}: could not fetch sessions: {e}')
            return
        playing_ids = {s['id'] for s in sessions}
        self.grace_logged = {x for x in self.grace_logged if x[0] in playing_ids}

        grace_sec = self.config.stop_grace_min * 60
        for s in sessions:
            sid, item_id = s['id'], s['item_id']
            desc = f"user {user_name}, session {sid} ({s['client']} on {s['device']}), '{s['item_name']}'"
            remaining = s['remaining_sec']
            if remaining is not None and remaining <= grace_sec:
                if (sid, item_id) not in self.grace_logged:
                    self.grace_logged.add((sid, item_id))
                    logger.info(f'stop skipped, {desc}: limit exceeded by {-time_left} min, '
                                f'but only {remaining / 60:.1f} min of the item left (grace {self.config.stop_grace_min} min)')
                continue
            if sid in self.stopping:
                continue  # stop loop already running for this session
            remaining_txt = f'{remaining / 60:.1f} min' if remaining is not None else 'unknown'
            logger.info(f'stopping playback, {desc}: limit exceeded by {-time_left} min, remaining {remaining_txt}')
            if not s['remote_control']:
                logger.warning(f'session does not support remote control, stop is likely to fail, {desc}')
            self.start_stop_loop(user_id, sid, desc)

    def start_stop_loop(self, user_id, sid, desc):
        """Starts a background loop that repeatedly stops the session (one loop per session at a time)."""
        if sid in self.stopping:
            return False
        self.stopping.add(sid)
        threading.Thread(target=self.stop_loop, args=(user_id, sid, desc), daemon=True).start()
        return True

    def stop_loop(self, user_id, sid, desc):
        """For up to STOP_LOOP_SEC: while the session is playing, send stop, sleep 1 s."""
        try:
            try:
                self.api.send_message(sid, i18n.t('stop_header'), i18n.t('stop_text'), self.config.notify_timeout_ms)
            except Exception as e:
                logger.warning(f'could not send stop message, {desc}: {e}')
            deadline = time_module.monotonic() + STOP_LOOP_SEC
            sent = 0
            while True:
                try:
                    playing = any(s['id'] == sid for s in self.api.get_playing_sessions(user_id))
                except Exception as e:
                    logger.warning(f'could not check session state, {desc}: {e}')
                    playing = True
                if not playing:
                    logger.info(f'playback stopped after {sent} stop command(s), {desc}')
                    return
                if time_module.monotonic() >= deadline:
                    logger.warning(f'playback still running after {STOP_LOOP_SEC} s and {sent} stop command(s), {desc}')
                    return
                try:
                    self.api.stop_session(sid)
                except Exception as e:
                    logger.error(f'stop command failed with exception, {desc}: {e}')
                sent += 1
                time_module.sleep(1)
        finally:
            self.stopping.discard(sid)

    def debug_stop_now(self, user_id):
        """Debug: starts the stop loop for all playing sessions of the user (no limit/grace checks)."""
        user_name = self.select_users.get(user_id, user_id)
        sessions = self.api.get_playing_sessions(user_id)
        if not sessions:
            return f'{user_name}: no playing session'
        results = []
        for s in sessions:
            desc = f"user {user_name}, session {s['id']} ({s['client']} on {s['device']}), '{s['item_name']}'"
            logger.info(f"debug stop now, {desc}, remote control: {s['remote_control']}")
            started = self.start_stop_loop(user_id, s['id'], desc)
            results.append(f"{s['device']}: {'stopping (up to 60 s)' if started else 'already stopping'}, "
                           f"remote control: {'yes' if s['remote_control'] else 'NO'}")
        return '; '.join(results)

    def debug_test_toast(self, user_id):
        """Debug: sends a test toast to all playing sessions of the user."""
        user_name = self.select_users.get(user_id, user_id)
        sessions = self.api.get_playing_sessions(user_id)
        if not sessions:
            return f'{user_name}: no playing session'
        results = []
        for s in sessions:
            try:
                ok = self.api.send_message(s['id'], i18n.t('warn_header'), i18n.t('warn_text', t='test'),
                                           self.config.notify_timeout_ms)
            except Exception as e:
                logger.error(f"debug toast to session {s['id']} failed: {e}")
                ok = False
            logger.info(f"debug test toast, user {user_name}, session {s['id']} ({s['device']}): {ok}")
            results.append(f"{s['device']}: {'toast sent' if ok else 'FAILED'}, remote control: {'yes' if s['remote_control'] else 'NO'}")
        return '; '.join(results)

    def notify_time_left(self, user_id, time_left):
        """Shows a toast on the user's playing media players when a 'minutes left' threshold is crossed."""
        warn_at = self.config.notify_warn_at
        if not self.config.notify_enabled or not warn_at:
            return
        sent = self.notified.setdefault(user_id, set())
        sent.intersection_update(w for w in warn_at if time_left <= w)  # re-arm thresholds above current time left
        pending = [w for w in warn_at if time_left <= w and w not in sent]
        if not pending:
            return
        if time_left <= 0:
            sent.update(pending)  # limit exceeded, nothing left to announce
            return
        try:
            session_ids = self.api.get_playing_session_ids(user_id)
        except Exception as e:
            logger.warning(f'could not fetch sessions for notification: {e}')
            return
        if not session_ids:
            return  # nobody is watching; keep pending, so the user is warned as soon as playback starts
        sent.update(pending)
        header = i18n.t('warn_header')
        text = i18n.t('warn_text', t=time_left)
        logger.info(f'notify user {user_id}: {time_left} min left, sessions: {len(session_ids)}')
        threading.Thread(target=self.send_toasts, args=(session_ids, header, text), daemon=True).start()

    def send_toasts(self, session_ids, header, text):
        cfg = self.config
        for i in range(cfg.notify_repeat):
            if i > 0:
                time_module.sleep(cfg.notify_repeat_interval)
            for session_id in session_ids:
                try:
                    self.api.send_message(session_id, header, text, cfg.notify_timeout_ms)
                except Exception as e:
                    logger.warning(f'could not send message to session {session_id}: {e}')

    def get_today_watched_min(self, user_id):
        now = datetime.today()
        date_start = now.strftime('%Y-%m-%d')
        date_end = (now + timedelta(1)).strftime('%Y-%m-%d')
        # date_start = '2024-11-09'
        # date_end = '2024-11-10'
        time = self.api.get_total_time_sec(user_id, date_start, date_end) // 60
        return time

    def disable_user(self, user_id, is_disabled: bool = False):
        self.api.disable_user(user_id, is_disabled)

    def reset_altered_limits(self):
        for user_id in self.select_users:
            self.user_data[user_id]['altered_limit'] = self.config.get_limit(user_id)
        self.notified.clear()

    def alter_limit(self, user_id, diff):
        current_limit = self.user_data[user_id]['altered_limit']
        self.user_data[user_id]['altered_limit'] = clip(current_limit + diff, 0, 360)

    def get_altered_limit(self, user_id):
        return self.user_data[user_id]['altered_limit']

    def enable_accounts(self):
        if self.config.account_enable_on_day_reset:
            for user_id in self.select_users:
                self.disable_user(user_id, False)

    def refresh_view(self, view, user_id):
        logger.debug('refresh_view started for {}'.format(self.select_users[user_id]))
        is_disabled = self.api.is_user_disabled(user_id)
        time_watched = self.get_today_watched_min(user_id)
        altered_limit = self.get_altered_limit(user_id)
        default_limit = self.config.get_limit(user_id)
        time_left = altered_limit - time_watched
        folders = self.user_data[user_id]['folders']

        if view['user_id'] != user_id:
            view['user_id'] = user_id
            view['user_link'] = f'{self.config.host}/web/#/dashboard/users/access?userId={user_id}'
            logger.debug('user changed for {}, limit: {}'.format(self.select_users[user_id], altered_limit))
        view['time_left'] = time_left
        view['time_watched_msg'] = i18n.t('watched', t=time_watched)
        view['time_left_msg'] = i18n.t('left', t=time_left) if time_left > 0 else i18n.t('exceed', t=-time_left)
        view['default_limit_msg'] = i18n.t('default', t=default_limit)
        view['altered_limit_msg'] = i18n.t('today', t=altered_limit)
        view['active_msg'] = i18n.t('disabled') if is_disabled else i18n.t('enabled')
        view['progress'] = min(time_watched / altered_limit, 1.0) if altered_limit > 0 else 1.0
        view['folders'] = "Folders:\n" + " \n".join(folders)
