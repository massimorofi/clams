"""GitHub integration: REST+GraphQL client, intake poller, outbound projection."""
from .client import GitHubClient, GitHubError  # noqa: F401
from .intake import run_intake  # noqa: F401
from . import projection  # noqa: F401
