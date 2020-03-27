#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import logging
import requests
import time
import uuid

from base64 import b64encode
from datetime import datetime
from flask import request, session, url_for

from .oauth2 import OAuth2Base
from ..utils import get_args

log = logging.getLogger(__name__)
args = get_args()


class DiscordAuth(OAuth2Base):

    def __init__(self):
        self.redirect_uri = args.server_uri + '/auth/discord'
        self.client_id = args.discord_client_id
        self.client_secret = args.discord_client_secret
        self.api_base_url = 'https://discordapp.com/api/v6'
        self.access_token_url = 'https://discordapp.com/api/oauth2/token'
        self.revoke_token_url = ('https://discordapp.com/api/oauth2/token/'
                                 'revoke')
        self.authorize_url = 'https://discordapp.com/api/oauth2/authorize'
        self.scope = 'identify guilds'

        self.fetch_role_guilds = []
        self.required_roles = []
        self.blacklisted_roles = []
        self.access_configs = []

        roles = args.discord_required_roles + args.discord_blacklisted_roles
        for role in roles:
            if ':' in role:
                guild_id = role.split(':')[0]
            else:
                # No guild specified, use first required guild.
                guild_id = args.discord_required_guilds[0]
            if guild_id not in self.fetch_role_guilds:
                self.fetch_role_guilds.append(guild_id)

        for role in args.discord_required_roles:
            if ':' in role:
                role_id = role.split(':')[0]
                guild_id = role.split(':')[1]
            else:
                # No guild specified, use first required guild.
                role_id = role
                guild_id = args.discord_required_guilds[0]
            self.required_roles.append((role_id, guild_id))

        for role in args.discord_blacklisted_roles:
            if ':' in role:
                role_id = role.split(':')[0]
                guild_id = role.split(':')[1]
            else:
                # No guild specified, use first required guild.
                role_id = role
                guild_id = args.discord_required_guilds[0]
            self.blacklisted_roles.append((role_id, guild_id))

        for elem in args.discord_access_configs:
            count = 0
            for c in elem:
                if c == ':':
                    count += 1
            if count == 1:
                role_id = None
                guild_id = elem.split(':')[0]
                config_name = elem.split(':')[1]
            elif count == 2:
                role_id = elem.split(':')[0]
                guild_id = elem.split(':')[1]
                config_name = elem.split(':')[2]
            self.access_configs.append((role_id, guild_id, config_name))

    def get_authorization_url(self):
        session['state'] = str(uuid.uuid4())
        auth_url = ('{}?response_type=code&client_id={}&scope={}&state={}&'
                    'redirect_uri={}&prompt=consent'.format(
                        self.authorize_url, self.client_id,
                        self.scope.replace(' ', '%20'), session['state'],
                        self.redirect_uri))
        return auth_url

    def authorize(self):
        if 'state' not in session:
            log.warning('Invalid Discord authorization attempt: '
                        'no state in session.')
            return

        state = request.args.get('state')
        if state != session['state']:
            log.warning('Invalid Discord authorization attempt: '
                        'incorrect state.')
            del session['state']
            return
        del session['state']

        error = request.args.get('error')
        if error is not None:
            if error == 'access_denied':
                log.debug('Discord authorization attempt denied, the resource '
                          'owner denied the request.')
            else:
                error_description = request.args.get('error_description', '')
                log.warning('Discord authorization attempt error: %s',
                            error_description)
            return

        code = request.args.get('code')
        if code is None:
            log.warning('Invalid Discord authorization attempt: '
                        'access code missing.')
            return
        try:
            token = self._exchange_code(code)
        except requests.exceptions.HTTPError as e:
            log.warning('Exception while retrieving Discord access token: %s',
                        e)
            return

        try:
            self._add_user(token)
        except requests.exceptions.HTTPError as e:
            log.warning('Exception while adding Discord user: %s', e)
            return
        session['auth_type'] = 'discord'
        log.debug('Discord user %s succesfully logged in.',
                  session['username'])

    def end_session(self):
        try:
            self._revoke_token(session['token']['access_token'])
        except requests.exceptions.HTTPError as e:
            log.warning('Exception while revoking Discord access token: %s', e)

        log.debug('Discord user %s succesfully logged out.',
                  session['username'])
        session.clear()

    def get_access_data(self):
        if session.get('access_data_updated_at', 0) + 300 < time.time():
            try:
                self._update_access_data()
            except requests.exceptions.HTTPError as e:
                if e.response.status_code in [400, 401]:
                    token = session['token']
                    if token['expires_at'] < time.time():
                        try:
                            token = self._refresh_token(token['refresh_token'])
                            session['token'] = {
                                'access_token': token['access_token'],
                                'refresh_token': token['refresh_token'],
                                'expires_at': (time.time() +
                                               token['expires_in'] - 5)
                            }

                            return self.get_access_data()
                        except requests.exceptions.HTTPError as e:
                            pass

                    # Token has (most likely) been revoked by user,
                    # log out the user.
                    session.clear()
                    return False, None, url_for('login_page')

                if 'has_permission' not in session:
                    # Access data is still missing, retry.
                    if e.response.status_code == 429:
                        # We are rate limited, wait a bit.
                        log.debug('Discord rate limit exceeded: %s', e)
                        s = int(e.response.headers['x-ratelimit-reset-after'])
                        time.sleep(s)
                    else:
                        log.warning('Exception while retrieving Discord '
                                    'resources: %s', e)

                    return self.get_access_data()

        has_permission = session['has_permission']
        redirect_uri = (args.discord_no_permission_redirect
                        if not has_permission else None)
        access_config_name = session['access_config_name']

        return has_permission, redirect_uri, access_config_name

    def _update_access_data(self):
        user_guilds = self._get_guilds()
        user_roles = {}
        for guild_id in self.fetch_role_guilds:
            roles = self._get_roles(guild_id, session['id'])
            user_roles[guild_id] = roles

        # Check required guilds.
        in_required_guild = False
        for guild_id in args.discord_required_guilds:
            if guild_id in user_guilds:
                in_required_guild = True
                break
        if len(args.discord_required_guilds) > 0 and not in_required_guild:
            session['has_permission'] = False
            session['access_config_name'] = None
            return

        # Check blacklisted guilds.
        for guild_id in args.discord_blacklisted_guilds:
            if guild_id in user_guilds:
                session['has_permission'] = False
                session['access_config_name'] = None
                return

        # Check required roles.
        has_required_role = False
        for role in self.required_roles:
            role_id = role[0]
            guild_id = role[1]
            if guild_id in user_guilds and role_id in user_roles[guild_id]:
                has_required_role = True
                break
        if len(self.required_roles) > 0 and not has_required_role:
            session['has_permission'] = False
            session['access_config_name'] = None
            return

        # Check blacklisted roles:
        for role in self.blacklisted_roles:
            role_id = role[0]
            guild_id = role[1]
            if guild_id in user_guilds and role_id in user_roles[guild_id]:
                session['has_permission'] = False
                session['access_config_name'] = None
                return

        access_config_name = None
        for elem in self.access_configs:
            role_id = elem[0]
            guild_id = elem[1]
            config_name = elem[2]

            if role_id is not None:
                if guild_id in user_guilds and role_id in user_roles[guild_id]:
                    access_config_name = config_name
                    break
            else:
                if guild_id in user_guilds:
                    access_config_name = config_name
                    break

        session['has_permission'] = True
        session['access_config_name'] = access_config_name
        session['access_data_updated_at'] = time.time()

    def _add_user(self, token):
        headers = {
            'Authorization': 'Bearer ' + token['access_token']
        }
        r = requests.get(self.api_base_url + '/users/@me', headers=headers)
        r.raise_for_status()
        user = r.json()

        session['id'] = user['id']
        session['username'] = user['username']
        session['token'] = {
            'access_token': token['access_token'],
            'refresh_token': token['refresh_token'],
            'expires_at': time.time() + token['expires_in'] - 5
        }

    def _exchange_code(self, code):
        data = {
            'client_id': self.client_id,
            'client_secret': self.client_secret,
            'grant_type': 'authorization_code',
            'code': code,
            'redirect_uri': self.redirect_uri,
            'scope': self.scope
        }
        headers = {
            'Content-Type': 'application/x-www-form-urlencoded'
        }
        r = requests.post(self.access_token_url, data=data, headers=headers)
        r.raise_for_status()
        return r.json()

    def _refresh_token(self, refresh_token):
        data = {
            'client_id': self.client_id,
            'client_secret': self.client_secret,
            'grant_type': 'refresh_token',
            'refresh_token': refresh_token,
            'redirect_uri': self.redirect_uri,
            'scope': self.scope
        }
        headers = {
            'Content-Type': 'application/x-www-form-urlencoded'
        }
        r = requests.post(self.access_token_url, data=data, headers=headers)
        r.raise_for_status()
        return r.json()

    def _revoke_token(self, access_token):
        data = self.client_id + ':' + self.client_secret
        bytes = b64encode(data.encode("utf-8"))
        encoded_creds = str(bytes, "utf-8")
        headers = {
          'Authorization': 'Basic ' + encoded_creds,
          'Content-Type': 'application/x-www-form-urlencoded'
        }
        data = 'token=' + access_token
        r = requests.post(self.revoke_token_url, data=data, headers=headers)
        r.raise_for_status()

    def _get_guilds(self):
        headers = {
            'Authorization': 'Bearer ' + session['token']['access_token']
        }
        r = requests.get(self.api_base_url + '/users/@me/guilds',
                         headers=headers)
        r.raise_for_status()
        guilds = r.json()
        guilds_dict = {}
        for guild in guilds:
            guilds_dict[guild['id']] = guild
        return guilds_dict

    def _get_roles(self, guild_id, user_id):
        headers = {
            'Authorization': 'Bot ' + args.discord_bot_token
        }
        r = requests.get(
            self.api_base_url + '/guilds/' + guild_id + '/members/' + user_id,
            headers=headers
        )
        r.raise_for_status()
        return r.json()['roles']
