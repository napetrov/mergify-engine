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

import daiquiri
import pkg_resources
import voluptuous
import yaml

from mergify_engine import check_api
from mergify_engine import config
from mergify_engine import context
from mergify_engine import rules
from mergify_engine import subscription
from mergify_engine.engine import actions_runner
from mergify_engine.engine import commands_runner


LOG = daiquiri.getLogger(__name__)

mergify_rule_path = pkg_resources.resource_filename(
    __name__, "../data/default_pull_request_rules.yml"
)

with open(mergify_rule_path, "r") as f:
    DEFAULT_PULL_REQUEST_RULES = voluptuous.Schema(rules.PullRequestRulesSchema)(
        yaml.safe_load(f.read())["rules"]
    )


def check_configuration_changes(ctxt):
    if ctxt.pull["base"]["repo"]["default_branch"] == ctxt.pull["base"]["ref"]:
        ref = None
        for f in ctxt.files:
            if f["filename"] in rules.MERGIFY_CONFIG_FILENAMES:
                ref = f["contents_url"].split("?ref=")[1]

        if ref is not None:
            try:
                rules.get_mergify_config(
                    ctxt.client, ctxt.pull["base"]["repo"]["name"], ref=ref
                )
            except rules.InvalidRules as e:
                # Not configured, post status check with the error message
                check_api.set_check_run(
                    ctxt,
                    actions_runner.SUMMARY_NAME,
                    "completed",
                    "failure",
                    output={
                        "title": "The new Mergify configuration is invalid",
                        "summary": str(e),
                        "annotations": e.get_annotations(e.filename),
                    },
                )
            else:
                check_api.set_check_run(
                    ctxt,
                    actions_runner.SUMMARY_NAME,
                    "completed",
                    "success",
                    output={
                        "title": "The new Mergify configuration is valid",
                        "summary": "This pull request must be merged "
                        "manually because it modifies Mergify configuration",
                    },
                )

            return True
    return False


def get_summary_from_sha(ctxt, sha):
    checks = check_api.get_checks_for_ref(
        ctxt,
        sha,
        check_name=actions_runner.SUMMARY_NAME,
    )
    checks = [c for c in checks if c["app"]["id"] == config.INTEGRATION_ID]
    if checks:
        return checks[0]


def get_summary_from_synchronize_event(ctxt):
    # NOTE(sileht): This solution has some design race, rare but present. Example:
    # * we receive /whatever/ event
    # * engine run for a PR
    # * github send synchronize in the meantime
    # * engine GET /pull/123
    #   here the head_sha is the new one (after synchronize occurs), but ctxt.sources does not
    #   have the synchronize event, so it can't retrieve the previous summary.
    #   Mergify thinks it's the first time it sees the PR, this introduces some bugs like:
    #   * PR not cleanup from queue
    #   * Comment posted twice
    #
    # Github does not offer API to retrieve previous PR head sha, the issues/123/events
    # have events for "synchronize" event coming from "force-push" only and not when new commits
    # are added.

    synchronize_events = dict(
        (
            (s["data"]["after"], s["data"])
            for s in ctxt.sources
            if s["event_type"] == "pull_request"
            and s["data"]["action"] == "synchronize"
            and "after" in s["data"]
        )
    )
    if synchronize_events:
        ctxt.log.debug("engine synchronize summary")

        # NOTE(sileht): We sometimes got multiple synchronize events in a row, that's not
        # always the last one that have the Summary, so we also looks in older ones if
        # necessary.
        after_sha = ctxt.pull["head"]["sha"]
        while synchronize_events:
            sync_event = synchronize_events.pop(after_sha, None)
            if sync_event:
                previous_summary = get_summary_from_sha(ctxt, sync_event["before"])
                if previous_summary:
                    return previous_summary

                after_sha = sync_event["before"]
            else:
                ctxt.log.warning(
                    "Got synchronize event but didn't find Summary on previous head sha",
                )
                break


def ensure_summary_on_head_sha(ctxt):
    for check in ctxt.pull_engine_check_runs:
        if check["name"] == actions_runner.SUMMARY_NAME:
            return

    sha = actions_runner.get_last_summary_head_sha(ctxt)
    if sha:
        previous_summary = get_summary_from_sha(ctxt, sha)
    else:
        previous_summary = get_summary_from_synchronize_event(ctxt)

    if previous_summary:
        check_api.set_check_run(
            ctxt,
            actions_runner.SUMMARY_NAME,
            "completed",
            "success",
            output={
                "title": previous_summary["output"]["title"],
                "summary": previous_summary["output"]["summary"],
            },
        )
        actions_runner.save_last_summary_head_sha(ctxt)


def run(client, pull, sub, sources):
    LOG.debug("engine get context")
    ctxt = context.Context(client, pull, sub)
    ctxt.log.debug("engine start processing context")

    issue_comment_sources = []

    for source in sources:
        if source["event_type"] == "issue_comment":
            issue_comment_sources.append(source)
        else:
            ctxt.sources.append(source)

    ctxt.log.debug("engine run pending commands")
    commands_runner.run_pending_commands_tasks(ctxt)

    if issue_comment_sources:
        ctxt.log.debug("engine handle commands")
        for source in issue_comment_sources:
            commands_runner.handle(
                ctxt,
                source["data"]["comment"]["body"],
                source["data"]["comment"]["user"],
            )

    if not ctxt.sources:
        return

    if ctxt.client.auth.permissions_need_to_be_updated:
        check_api.set_check_run(
            ctxt,
            "Summary",
            "completed",
            "failure",
            output={
                "title": "Required GitHub permissions are missing.",
                "summary": "You can accept them at https://dashboard.mergify.io/",
            },
        )
        return

    ctxt.log.debug("engine check configuration change")
    if check_configuration_changes(ctxt):
        ctxt.log.info("Configuration changed, ignoring")
        return

    ctxt.log.debug("engine get configuration")
    # BRANCH CONFIGURATION CHECKING
    try:
        filename, mergify_config = rules.get_mergify_config(
            ctxt.client, ctxt.pull["base"]["repo"]["name"]
        )
    except rules.NoRules:  # pragma: no cover
        ctxt.log.info("No need to proceed queue (.mergify.yml is missing)")
        return
    except rules.InvalidRules as e:  # pragma: no cover
        # Not configured, post status check with the error message
        if any(
            (
                s["event_type"] == "pull_request"
                and s["data"]["action"] in ["opened", "synchronize"]
                for s in ctxt.sources
            )
        ):
            check_api.set_check_run(
                ctxt,
                actions_runner.SUMMARY_NAME,
                "completed",
                "failure",
                output={
                    "title": "The Mergify configuration is invalid",
                    "summary": str(e),
                    "annotations": e.get_annotations(e.filename),
                },
            )
        return

    # Add global and mandatory rules
    mergify_config["pull_request_rules"].rules.extend(DEFAULT_PULL_REQUEST_RULES.rules)

    if ctxt.pull["base"]["repo"]["private"] and not ctxt.subscription.has_feature(
        subscription.Features.PRIVATE_REPOSITORY
    ):
        check_api.set_check_run(
            ctxt,
            actions_runner.SUMMARY_NAME,
            "completed",
            "failure",
            output={
                "title": "Mergify is disabled",
                "summary": sub.reason,
            },
        )
        return

    ensure_summary_on_head_sha(ctxt)

    # NOTE(jd): that's fine for now, but I wonder if we wouldn't need a higher abstraction
    # to have such things run properly. Like hooks based on events that you could
    # register. It feels hackish otherwise.
    if any(
        s["event_type"] == "pull_request" and s["data"]["action"] == "closed"
        for s in ctxt.sources
    ):
        actions_runner.delete_last_summary_head_sha(ctxt)

    ctxt.log.debug("engine handle actions")
    actions_runner.handle(mergify_config["pull_request_rules"], ctxt)
