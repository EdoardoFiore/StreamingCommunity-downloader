import re

from random_user_agent.user_agent import UserAgent
from random_user_agent.params import SoftwareName, OperatingSystem

# Windows forbids \ / : * ? " < > | ; POSIX only forbids / and NUL. The filter is
# applied on every platform regardless: titles come from third-party sites and
# end up in filesystem paths, so a title containing "../" must not become a
# traversal just because the container happens to run Linux.
_FORBIDDEN = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


def sanitize_filename(name: str) -> str:
    cleaned = _FORBIDDEN.sub("", str(name)).strip().strip(".")
    # "." and ".." survive the character filter but are not usable names.
    if not cleaned or set(cleaned) <= {"."}:
        return "senza-nome"
    return cleaned[:180]


def get_headers():
    software_names = [SoftwareName.CHROME.value]
    operating_systems = [OperatingSystem.WINDOWS.value, OperatingSystem.LINUX.value]
    user_agent_rotator = UserAgent(software_names=software_names, operating_systems=operating_systems, limit=10)
    return user_agent_rotator.get_random_user_agent()
