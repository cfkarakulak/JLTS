Installation:
x

```
make install
```    

Remove everything about docker:

```
#!/bin/bash
set -e

# Stop all containers
containers=`docker ps -aq`
if [ -n "$containers" ] ; then
  docker stop $containers
fi

# Delete all containers
containers=`docker ps -aq`
if [ -n "$containers" ]; then
  docker rm -f -v $containers
fi

# Delete all images
images=`docker images -aq`
if [ -n "$images" ]; then
  docker rmi -f $images
fi

# Delete all volumes
volumes=`docker volume ls -qf dangling="true"`
if [ -n "$volumes" ]; then
  docker volume rm -f $volumes
fi

# Delete all networks
networks=`docker network ls -q`
if [ -n "$networks" ]; then
  docker network rm $networks
fi
```
