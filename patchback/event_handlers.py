"""Webhook event handlers."""

import http
import logging
import pathlib
import tempfile
from datetime import datetime

from anyio import run_in_thread
from gidgethub import BadRequest, ValidationError
from pygit2 import (
    clone_repository, GitError,
    RemoteCallbacks, Signature, UserPass,
)

from octomachinery.app.routing import process_event_actions
from octomachinery.app.routing.decorators import process_webhook_payload
from octomachinery.app.runtime.context import RUNTIME_CONTEXT


logger = logging.getLogger(__name__)

BACKPORT_LABEL_PREFIX = 'backport-'
BACKPORT_LABEL_LEN = len(BACKPORT_LABEL_PREFIX)


def ensure_pr_merged(event_handler):
    async def event_handler_wrapper(*, number, pull_request, **kwargs):
        if not pull_request['merged']:
            logger.info('PR#%s is not merged, ignoring...', number)
            return

        return await event_handler(
            number=number,
            pull_request=pull_request,
            **kwargs,
        )
    return event_handler_wrapper


def backport_pr_sync(
        pr_number: int, merge_commit_sha: str, target_branch: str,
        repo_slug: str, repo_remote: str, installation_access_token: str,
) -> None:
    """Returns a branch with backported PR pushed to GitHub.

    It clones the ``repo_remote`` using a GitHub App Installation token
    ``installation_access_token`` to authenticate. Then, it cherry-picks
    ``merge_commit_sha`` onto a new branch based on the
    ``target_branch`` and pushes it back to ``repo_remote``.
    """
    backport_pr_branch = (
        f'patchback/backports/{target_branch}/'
        f'{merge_commit_sha}/pr{pr_number}'
    )
    token_auth_callbacks = RemoteCallbacks(
        credentials=UserPass(
            'x-access-token', installation_access_token,
        ),
    )
    with tempfile.TemporaryDirectory(
            prefix=f'{repo_slug.replace("/", "--")}---{target_branch}---',
            suffix=f'---PR-{pr_number}.git',
    ) as tmp_dir:
        logger.info('Created a temporary dir: `%s`', tmp_dir)
        try:
            repo = clone_repository(
                url=repo_remote,
                path=pathlib.Path(tmp_dir),
                bare=True,
                # TODO: figure out if using "remote" would be cleaner:
                # remote=,  # callable (Repository, name, url) -> Remote
                callbacks=token_auth_callbacks,
            )
        except KeyError as key_err:
            raise LookupError(
                f'Failed to check out branch {target_branch}',
            ) from key_err
        else:
            logger.info('Checked out `%s@%s`', repo_remote, target_branch)
        repo.remotes.add_fetch(  # phantom merge heads
            'origin',
            # '+refs/pull/*/merge:refs/merge/origin/*',
            f'+refs/pull/{pr_number}/merge:refs/merge/origin/{pr_number}',
        )
        repo.remotes.add_fetch(  # read-only PR branch heads
            'origin',
            # '+refs/pull/*/head:refs/pull/origin/*',
            f'+refs/pull/{pr_number}/head:refs/pull/origin/{pr_number}',
        )
        github_upstream_remote = repo.remotes['origin']
        github_upstream_remote.fetch(callbacks=token_auth_callbacks)
        logger.info('Fetched read-only PR refs')

        repo.remotes.add_push(  # PR backport branch
            'origin',
            ':'.join((target_branch, backport_pr_branch))
        )

        logger.info(
            'Cherry-picking `%s` into `%s`',
            merge_commit_sha, backport_pr_branch,
        )
        # Ref: https://www.pygit2.org/recipes/git-cherry-pick.html
        cherry = repo.revparse_single(merge_commit_sha)
        backport_branch = repo.branches.local.create(
            backport_pr_branch,
            repo.branches[f'origin/{target_branch}'].peel(),
        )

        base = repo.merge_base(cherry.oid, backport_branch.target)
        base_tree = cherry.parents[0].tree

        index = repo.merge_trees(base_tree, backport_branch, cherry)
        tree_id = index.write_tree(repo)

        author = cherry.author
        committer = Signature('Patchback', 'patchback@sanitizers.bot')

        repo.create_commit(
            backport_branch.name,
            author, committer,
            cherry.message,
            tree_id,
            [backport_branch.target],
        )
        logger.info('Backported the commit into `%s`', backport_pr_branch)
        logger.info('Pushing `%s` back to GitHub...', backport_pr_branch)
        try:
            github_upstream_remote.push(
                [f'HEAD:refs/heads/{backport_pr_branch}'],
                callbacks=token_auth_callbacks,  # clone callbacks aren't preserved
            )
        except GitError as pg2_err:
            if str(pg2_err) != 'unexpected http status code: 403':
                raise
            raise PermissionError(
                'Current GitHub App installation does not grant sufficient '
                f'privileges for pushing to {repo_remote}. `Contents: '
                'write` permission is necessary to fix this.',
            ) from pg2_err
        else:
            logger.info('Push to GitHub succeeded...')

    return backport_pr_branch


@process_event_actions('pull_request', {'closed'})
@process_webhook_payload
@ensure_pr_merged
async def on_merge_of_labeled_pr(
        *,
        number,  # PR number
        pull_request,  # PR details subobject
        repository,  # repo details subobject
        **_kwargs,  # unimportant event details
) -> None:
    """React to labeled pull request merge."""
    labels = [label['name'] for label in pull_request['labels']]
    target_branches = [
        label[BACKPORT_LABEL_LEN:] for label in labels
        if label.startswith(BACKPORT_LABEL_PREFIX)
    ]

    if not target_branches:
        logger.info('PR#%s does not have backport labels, ignoring...', number)
        return

    merge_commit_sha = pull_request['merge_commit_sha']

    logger.info(
        'PR#%s is labeled with "%s". It needs to be backported to %s',
        number, labels, ', '.join(target_branches),
    )
    logger.info('PR#%s merge commit: %s', number, merge_commit_sha)

    for target_branch in target_branches:
        await process_pr_backport_labels(
            number,
            pull_request['title'],
            pull_request['body'],
            pull_request['base']['ref'],
            pull_request['head']['ref'],
            merge_commit_sha,
            target_branch,
            repository['pulls_url'],
            repository['full_name'],
            repository['clone_url'],
        )


@process_event_actions('pull_request', {'labeled'})
@process_webhook_payload
@ensure_pr_merged
async def on_label_added_to_merged_pr(
        *,
        label,  # label added
        number,  # PR number
        pull_request,  # PR details subobject
        repository,  # repo details subobject
        **_kwargs,  # unimportant event details
) -> None:
    """React to GitHub App pull request / issue label webhook event."""
    label_name = label['name']
    if not label_name.startswith(BACKPORT_LABEL_PREFIX):
        logger.info(
            'PR#%s got labeled with %s but it is not '
            'a backport label, ignoring...',
            number, label_name,
        )
        return

    target_branch = label_name[BACKPORT_LABEL_LEN:]
    merge_commit_sha = pull_request['merge_commit_sha']

    logger.info(
        'PR#%s got labeled with "%s". It needs to be backported to %s',
        number, label_name, target_branch,
    )
    logger.info('PR#%s merge commit: %s', number, merge_commit_sha)
    await process_pr_backport_labels(
        number,
        pull_request['title'],
        pull_request['body'],
        pull_request['base']['ref'],
        pull_request['head']['ref'],
        merge_commit_sha,
        target_branch,
        repository['pulls_url'],
        repository['full_name'],
        repository['clone_url'],
    )


async def process_pr_backport_labels(
        pr_number,
        pr_title,
        pr_body,
        pr_base_ref,
        pr_head_ref,
        pr_merge_commit,
        target_branch,
        pr_api_url, repo_slug,
        git_url,
) -> None:
    gh_api = RUNTIME_CONTEXT.app_installation_client
    check_runs_base_uri = f'/repos/{repo_slug}/check-runs'
    check_run_name = f'Backport to {target_branch}'
    use_checks_api = False

    try:
        checks_resp = await gh_api.post(
            check_runs_base_uri,
            preview_api_version='antiope',
            data={
                'name': check_run_name,
                # NOTE: We don't use "pr_merge_commit" because then the
                # NOTE: check would only show up on the merge commit but
                # NOTE: not in PR. PRs only show checks from PR branch
                # NOTE: HEAD. This is a bit imprecise but
                # NOTE: it is what it is.
                'head_sha': pr_head_ref,
                'status': 'queued',
                'started_at': f'{datetime.utcnow().isoformat()}Z',
            },
        )
    except BadRequest as bad_req_err:
        if (
                bad_req_err.status_code != http.client.FORBIDDEN or
                str(bad_req_err) != 'Resource not accessible by integration'
        ):
            raise
        logger.info(
            'Failed to report PR #%d (commit `%s`) backport status updates via Checks API because '
            'of insufficient GitHub App Installation privileges to '
            'create pull requests: %s',
            pr_number, pr_merge_commit, bad_req_err,
        )
    else:
        check_runs_updates_uri = f'{check_runs_base_uri}/{checks_resp["id"]:d}'
        use_checks_api = True
        logger.info('Checks API is available')

    if use_checks_api:
        await gh_api.patch(
            check_runs_updates_uri,
            preview_api_version='antiope',
            data={
                'name': check_run_name,
                'status': 'in_progress',
            },
        )

    try:
        backport_pr_branch = await run_in_thread(
            backport_pr_sync,
            pr_number,
            pr_merge_commit,
            target_branch,
            repo_slug,
            git_url,
            (await RUNTIME_CONTEXT.app_installation.get_token()).token,
        )
    except LookupError as lu_err:
        logger.info(
            'Failed to backport PR #%d (commit `%s`) to `%s` '
            'because the target branch does not exist',
            pr_number, pr_merge_commit, target_branch,
        )
        if not use_checks_api:
            return
        await gh_api.patch(
            check_runs_updates_uri,
            preview_api_version='antiope',
            data={
                'name': check_run_name,
                'status': 'completed',
                'conclusion': 'neutral',
                'completed_at': f'{datetime.utcnow().isoformat()}Z',
                'output': {
                    'title': f'{check_run_name}: cherry-picking failed '
                    '— target branch does not exist',
                    'text': f'',
                    'summary': str(lu_err),
                },
            },
        )
        return
    except PermissionError as perm_err:
        logger.info(
            'Failed to backport PR #%d (commit `%s`) to `%s` because '
            'of insufficient GitHub App Installation privileges to '
            'modify the repo contents',
            pr_number, pr_merge_commit, target_branch,
        )
        if not use_checks_api:
            return
        await gh_api.patch(
            check_runs_updates_uri,
            preview_api_version='antiope',
            data={
                'name': check_run_name,
                'status': 'completed',
                'conclusion': 'neutral',
                'completed_at': f'{datetime.utcnow().isoformat()}Z',
                'output': {
                    'title': f'{check_run_name}: cherry-picking failed '
                    '— could not push',
                    'text': f'',
                    'summary': str(perm_err),
                },
            },
        )
        return
    else:
        logger.info('Backport PR branch: `%s`', backport_pr_branch)

    if use_checks_api:
        await gh_api.patch(
            check_runs_updates_uri,
            preview_api_version='antiope',
            data={
                'name': check_run_name,
                'status': 'in_progress',
                'output': {
                    'title': f'{check_run_name}: cherry-pick succeeded',
                    'text': 'PR branch created, proceeding with making a PR.',
                    'summary': f'Backport PR branch: `{backport_pr_branch}',
                },
            },
        )

    logger.info('Creating a backport PR...')
    try:
        pr_resp = await gh_api.post(
            pr_api_url,
            data={
                'title': f'[PR #{pr_number}/{pr_merge_commit[:8]} backport]'
                f'[{target_branch}] {pr_title}',
                'head': backport_pr_branch,
                'base': target_branch,
                'body': f'**This is a backport of PR #{pr_number} as '
                f'merged into {pr_base_ref} '
                f'({pr_merge_commit}).**\n\n{pr_body}',
                'maintainer_can_modify': True,
                'draft': False,
            },
        )
    except ValidationError as val_err:
        logger.info(
            'Failed to backport PR #%d (commit `%s`) to `%s`: %s',
            pr_number, pr_merge_commit, target_branch, val_err,
        )
        if not use_checks_api:
            return
        await gh_api.patch(
            check_runs_updates_uri,
            preview_api_version='antiope',
            data={
                'name': check_run_name,
                'status': 'completed',
                'conclusion': 'neutral',
                'completed_at': f'{datetime.utcnow().isoformat()}Z',
                'output': {
                    'title': f'{check_run_name}: creation of the '
                    'backport PR failed',
                    'text': '',
                    'summary': f'Backport PR branch: `{backport_pr_branch}\n\n'
                    f'{val_err!s}',
                },
            },
        )
        return
    except BadRequest as bad_req_err:
        if (
                bad_req_err.status_code != http.client.FORBIDDEN or
                str(bad_req_err) != 'Resource not accessible by integration'
        ):
            raise
        logger.info(
            'Failed to backport PR #%d (commit `%s`) to `%s` because '
            'of insufficient GitHub App Installation privileges to '
            'create pull requests',
            pr_number, pr_merge_commit, target_branch,
        )
        if not use_checks_api:
            return
        await gh_api.patch(
            check_runs_updates_uri,
            preview_api_version='antiope',
            data={
                'name': check_run_name,
                'status': 'completed',
                'conclusion': 'neutral',
                'completed_at': f'{datetime.utcnow().isoformat()}Z',
                'output': {
                    'title': f'{check_run_name}: creation of the '
                    'backport PR failed',
                    'text': '',
                    'summary': f'Backport PR branch: `{backport_pr_branch}\n\n'
                    f'{bad_req_err!s}',
                },
            },
        )
        return
    else:
        logger.info('Created a PR @ %s', pr_resp['html_url'])

    if use_checks_api:
        await gh_api.patch(
            check_runs_updates_uri,
            preview_api_version='antiope',
            data={
                'name': check_run_name,
                'status': 'completed',
                'conclusion': 'success',
                'completed_at': f'{datetime.utcnow().isoformat()}Z',
                'output': {
                    'title': f'{check_run_name}: backport PR created',
                    'text': f'Backported as {pr_resp["html_url"]}',
                    'summary': f'Backport PR branch: `{backport_pr_branch}',
                },
            },
        )
