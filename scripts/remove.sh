#!/bin/bash
set -e

docker stop 25d1965e0b06 12b3724346e8
docker rm -f -v 25d1965e0b06 12b3724346e8
docker rmi -f eb05ce36224f
docker network rm bitnami-docker-python_default

echo "Docker residues have been motherfucking purged boss."
