FROM docker.io/bitnami/minideb:buster
LABEL maintainer "Bitnami <containers@bitnami.com>"

# Install required system packages and dependencies
RUN install_packages build-essential ca-certificates curl git libbz2-1.0 libc6 libffi6 liblzma5 libncursesw6 libreadline7 libsqlite3-0 libsqlite3-dev libssl-dev libssl1.1 libtinfo6 pkg-config procps sudo unzip wget zlib1g
RUN wget -nc -P /tmp/bitnami/pkg/cache/ https://downloads.bitnami.com/files/stacksmith/python-3.8.2-1-linux-amd64-debian-10.tar.gz && \
    echo "f2b2cfb905db1284159ec2268bad4d3c59138a00f33b5710df6d8524d5f44ce7  /tmp/bitnami/pkg/cache/python-3.8.2-1-linux-amd64-debian-10.tar.gz" | sha256sum -c - && \
    tar -zxf /tmp/bitnami/pkg/cache/python-3.8.2-1-linux-amd64-debian-10.tar.gz -P --transform 's|^[^/]*/files|/opt/bitnami|' --wildcards '*/files' && \
    rm -rf /tmp/bitnami/pkg/cache/python-3.8.2-1-linux-amd64-debian-10.tar.gz
RUN apt-get update && apt-get upgrade -y && \
    rm -r /var/lib/apt/lists /var/cache/apt/archives
RUN sed -i 's/^PASS_MAX_DAYS.*/PASS_MAX_DAYS    90/' /etc/login.defs && \
    sed -i 's/^PASS_MIN_DAYS.*/PASS_MIN_DAYS    0/' /etc/login.defs && \
    sed -i 's/sha512/sha512 minlen=8/' /etc/pam.d/common-password

# Supervisor
RUN apt-get update \
 && apt-get install -y --no-install-recommends supervisor \
 && apt-get install -y cron

# Copy supervisor file in the supervisor directory
COPY ./docker/supervisor/supervisord.conf /etc/supervisor
COPY ./docker/supervisor/conf.d/* /etc/supervisor/conf.d/

# Copy crontab file in the cron directory
ADD ./docker/cron/* /etc/cron.d/

# Give execution rights on the cron job
RUN chmod 0644 /etc/cron.d/*

# Create the log file to be able to run tail
RUN touch /var/log/cron.log

ENV BITNAMI_APP_NAME="python" \
    BITNAMI_IMAGE_VERSION="3.8.2-debian-10-r43" \
    PATH="/opt/bitnami/python/bin:$PATH"
EXPOSE 8000

WORKDIR /app

# Install dependencies:
COPY ./src/requirements.txt .
RUN pip install -r requirements.txt

# Add start script and grant execution rights
ADD ./docker/start.sh /scripts/start.sh
RUN chmod +x /scripts/start.sh

CMD ["/scripts/start.sh"]
