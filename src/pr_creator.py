"""Open a GitHub pull request for a generated fix.

Authenticates as the GitHub App installation on the target repo (not a
personal access token), creates a branch, pushes the fixed file content
directly via the Contents API (not by applying a text diff — we already
have the exact new content from claude_fixer, so there's nothing to parse
or risk a patch-apply conflict on), and opens a PR.

Never merges. Per CLAUDE.md: every fix defaults to hold-for-human-review.
"""
import os
import re
import logging
from typing import Optional
from github import Auth, Github, GithubIntegration
from github.GithubException import GithubException

logger = logging.getLogger(__name__)


def _slugify(text: str, max_len: int = 50) -> str:
    """Turn a change title into a safe branch-name fragment."""
    slug = re.sub(r'[^a-z0-9]+', '-', text.lower()).strip('-')
    return slug[:max_len].rstrip('-') or 'change'


def get_installation_client(owner: str, repo: str,
                             app_id: Optional[str] = None,
                             private_key: Optional[str] = None) -> Optional[Github]:
    """
    Authenticate as the GitHub App installed on owner/repo and return a
    Github client scoped to that installation (not a user/personal token).

    Args:
        owner, repo: The target repository
        app_id, private_key: Defaults to GITHUB_APP_ID / GITHUB_PRIVATE_KEY
            from the environment if not passed explicitly

    Returns:
        An authenticated Github client, or None if auth fails (logged).
    """
    app_id = app_id or os.getenv('GITHUB_APP_ID')
    private_key = private_key or os.getenv('GITHUB_PRIVATE_KEY')

    if not app_id or not private_key:
        logger.error("GITHUB_APP_ID / GITHUB_PRIVATE_KEY not set - cannot authenticate")
        return None

    try:
        auth = Auth.AppAuth(app_id, private_key)
        integration = GithubIntegration(auth=auth)
        installation = integration.get_repo_installation(owner, repo)
        return integration.get_github_for_installation(installation.id)
    except GithubException as e:
        logger.error(f"Failed to authenticate GitHub App for {owner}/{repo}: {e}")
        return None


def create_fix_pr(client: Github, owner: str, repo: str, fix: dict,
                   base_branch: str = 'main') -> Optional[dict]:
    """
    Create a branch, push the fixed file(s), and open a PR.

    Args:
        client: Authenticated Github client (see get_installation_client)
        owner, repo: Target repository
        fix: A result from claude_fixer.generate_fix_for_changes() —
             needs 'change', 'fixed_files', 'pr_description'
        base_branch: Branch to open the PR against

    Returns:
        {'url': str, 'number': int, 'branch': str} on success, or None if
        there was nothing to do / it failed (logged either way).
    """
    change = fix['change']
    fixed_files = fix.get('fixed_files', {})

    if not fixed_files:
        logger.warning(f"No fixed files for '{change.get('title')}' - nothing to open a PR for")
        return None

    try:
        gh_repo = client.get_repo(f"{owner}/{repo}")
        base = gh_repo.get_branch(base_branch)
    except GithubException as e:
        logger.error(f"Failed to load {owner}/{repo}@{base_branch}: {e}")
        return None

    change_id = change.get('id') or _slugify(change.get('title', 'change'))
    branch_name = f"api-watchdog/{_slugify(str(change_id))}"

    try:
        gh_repo.create_git_ref(ref=f"refs/heads/{branch_name}", sha=base.commit.sha)
        logger.info(f"Created branch {branch_name}")
    except GithubException as e:
        if e.status == 422:
            # Branch already exists — most likely a re-run for a change
            # whose PR is still open. Reuse it rather than fail.
            logger.info(f"Branch {branch_name} already exists, reusing it")
        else:
            logger.error(f"Failed to create branch {branch_name}: {e}")
            return None

    commit_message = f"Fix: {change.get('title', 'Stripe API change')}"
    for file_path, contents in fixed_files.items():
        # GitHub's Contents API wants forward-slash, repo-relative paths.
        repo_path = file_path.replace('\\', '/').lstrip('./')
        try:
            existing = gh_repo.get_contents(repo_path, ref=branch_name)
            gh_repo.update_file(
                path=repo_path,
                message=commit_message,
                content=contents['new'],
                sha=existing.sha,
                branch=branch_name,
            )
            logger.info(f"Updated {repo_path} on {branch_name}")
        except GithubException as e:
            logger.error(f"Failed to update {repo_path} on {branch_name}: {e}")
            return None

    try:
        existing_prs = gh_repo.get_pulls(state='open', head=f"{owner}:{branch_name}", base=base_branch)
        existing_pr = next(iter(existing_prs), None)
        if existing_pr:
            logger.info(f"PR already open for {branch_name}: {existing_pr.html_url}")
            return {'url': existing_pr.html_url, 'number': existing_pr.number, 'branch': branch_name}

        pr = gh_repo.create_pull(
            title=f"Fix: {change.get('title', 'Stripe API change')}",
            body=fix.get('pr_description', ''),
            head=branch_name,
            base=base_branch,
        )
        logger.info(f"Opened PR #{pr.number}: {pr.html_url}")
        return {'url': pr.html_url, 'number': pr.number, 'branch': branch_name}
    except GithubException as e:
        # A timeout or 5xx here doesn't mean the PR wasn't created — GitHub
        # can succeed server-side after the client gives up waiting. Check
        # before reporting failure, rather than risk silently dropping a PR
        # that actually exists (or double-creating one on a naive retry).
        logger.warning(f"create_pull request failed ({e}), checking whether it succeeded anyway")
        try:
            retry_prs = gh_repo.get_pulls(state='open', head=f"{owner}:{branch_name}", base=base_branch)
            retry_pr = next(iter(retry_prs), None)
            if retry_pr:
                logger.info(f"PR was actually created despite the error: #{retry_pr.number}: {retry_pr.html_url}")
                return {'url': retry_pr.html_url, 'number': retry_pr.number, 'branch': branch_name}
        except GithubException:
            pass
        logger.error(f"Failed to open PR for {branch_name}: {e}")
        return None


def create_prs_for_fixes(owner: str, repo: str, fixes: list,
                          base_branch: str = 'main') -> list:
    """
    Open a PR for each generated fix against owner/repo.

    Args:
        owner, repo: Target repository
        fixes: Results from claude_fixer.generate_fix_for_changes()
        base_branch: Branch to open PRs against

    Returns:
        List of {'url', 'number', 'branch'} for each PR successfully opened
        (or reused). Fixes that failed to produce a PR are skipped, not
        included as failures — check the logs for why.
    """
    client = get_installation_client(owner, repo)
    if client is None:
        return []

    results = []
    for fix in fixes:
        result = create_fix_pr(client, owner, repo, fix, base_branch=base_branch)
        if result:
            results.append(result)

    logger.info(f"Opened/reused {len(results)} PR(s) out of {len(fixes)} fix(es)")
    return results
