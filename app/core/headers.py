import os
import re

from random_user_agent.user_agent import UserAgent
from random_user_agent.params import SoftwareName, OperatingSystem


def sanitize_filename(name: str) -> str:
    if os.name == 'nt':
        # Windows forbids: \ / : * ? " < > |
        return re.sub(r'[\\/:*?"<>|]', '', name).strip()
    return name


def get_headers():
    software_names = [SoftwareName.CHROME.value]
    operating_systems = [OperatingSystem.WINDOWS.value, OperatingSystem.LINUX.value]
    user_agent_rotator = UserAgent(software_names=software_names, operating_systems=operating_systems, limit=10)
    return user_agent_rotator.get_random_user_agent()
