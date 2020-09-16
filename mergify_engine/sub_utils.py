# -*- encoding: utf-8 -*-
#
# Copyright © 2018–2020 Mergify SAS
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.
import json

import daiquiri

from mergify_engine import config
from mergify_engine import crypto
from mergify_engine import utils
from mergify_engine.clients import http


LOG = daiquiri.getLogger(__name__)


async def _retrieve_subscription_from_db(owner_id):
    LOG.info("Subscription not cached, retrieving it...", gh_owner=owner_id)
    async with http.AsyncClient() as client:
        try:
            resp = await client.get(
                f"{config.SUBSCRIPTION_BASE_URL}/engine/github-account/{owner_id}",
                auth=(config.OAUTH_CLIENT_ID, config.OAUTH_CLIENT_SECRET),
            )
        except http.HTTPNotFound as e:
            sub = {
                "tokens": {},
                "subscription_active": False,
                "subscription_reason": e.message,
            }
        else:
            sub = resp.json()
            sub["tokens"] = dict(
                (login, token["access_token"]) for login, token in sub["tokens"].items()
            )
    return sub


async def _retrieve_subscription_from_cache(owner_id):
    r = await utils.get_aredis_for_cache()
    encrypted_sub = await r.get("subscription-cache-owner-%s" % owner_id)
    if encrypted_sub:
        return json.loads(crypto.decrypt(encrypted_sub).decode())


async def save_subscription_to_cache(owner_id, sub):
    r = await utils.get_aredis_for_cache()
    encrypted = crypto.encrypt(json.dumps(sub).encode())
    await r.setex("subscription-cache-owner-%s" % owner_id, 3600, encrypted)


async def get_subscription(owner_id):
    sub = await _retrieve_subscription_from_cache(owner_id)
    if sub is None:
        sub = await _retrieve_subscription_from_db(owner_id)
        await save_subscription_to_cache(owner_id, sub)
    return sub
