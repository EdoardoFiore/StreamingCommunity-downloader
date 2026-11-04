FROM python:3.11-alpine

# Only the clients: the daemon they talk to is the host's, through the mounted
# socket. See updater.py for why this is a service of its own.
RUN apk add --no-cache docker-cli docker-cli-compose

COPY updater.py /updater.py

# The volume shared with the panel starts from this directory's mode, so a panel
# running under any `user:` can reach the socket inside it (#28).
RUN mkdir -p /run/scpanel-updater && chmod 755 /run/scpanel-updater

CMD ["python", "-u", "/updater.py"]
