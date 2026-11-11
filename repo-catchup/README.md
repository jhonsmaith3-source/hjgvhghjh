# repo-catchup

A Mistral Workflow that catches you up on a GitHub repository: given a repo handle
(`owner/name` or a GitHub URL) and a cutoff date (`YYYY-MM-DD`), it lists every pull
request merged since that date and every open PR still waiting on review, summarizes
each in one line, and posts the digest to your Slack self-chat (DM to yourself).

## Workflow

- Name: `repo-catchup`
- Input: `{"repo": "owner/name", "since": "2025-11-01"}`
- Connectors: `github_app` (list PRs) and `slack` (own user info, send message)
- The workflow is on-behalf-of (`on_behalf_of=True`): it reads GitHub and posts to
  Slack with **the triggering user's** connector credentials, so it requires a
  **hardened** deployment.

## Setup

1. In Studio → Context → Connectors, add your credentials for the `github_app` and
   `slack` connectors (Credentials tab).
2. Install the Mistral GitHub App on the repository that hosts this code and connect
   GitHub in Studio → GitHub App settings (needed for the managed deployment build).
3. Create the managed deployment (hardened) pointing at this repository, e.g.:

   ```python
   client.workflows.deployments.create_deployment(
       name="repo-catchup",
       spec={
           "github_url": "https://github.com/<you>/<this-repo>",
           "revision": "main",
           "backend_spec": {"type": "mistral_cloud", "dockerfile_path": "Dockerfile"},
       },
   )
   ```

   Managed deployments created with `hardened=True` are required for the OBO workflow
   to register.

4. Run it:

   ```python
   execution = client.workflows.execute_workflow(
       workflow_identifier="repo-catchup",
       input={"repo": "mistralai/client-python", "since": "2025-11-01"},
       wait_for_result=True,
   )
   ```

## Notes

- "Waiting on review" means open, non-draft pull requests (the GitHub list API does
  not expose review state directly; review-level filtering would need one extra call
  per PR).
- Local development: set `MISTRAL_API_KEY` in `.env` and run `mistral dev`.
