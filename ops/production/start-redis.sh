#!/bin/sh
set -eu
umask 077
case "$REDIS_PASSWORD" in *[!a-f0-9]*|'') echo 'Redis requires a generated hex password.' >&2; exit 1;; esac
[ "${#REDIS_PASSWORD}" = 64 ] || exit 1
{
  printf '%s\n' 'bind 0.0.0.0' 'protected-mode yes' 'appendonly yes' 'appendfsync everysec'
  printf '%s\n' 'maxmemory 384mb' 'maxmemory-policy noeviction'
  printf 'requirepass %s\n' "$REDIS_PASSWORD"
} > /tmp/dataflow-redis.conf
chown redis:redis /tmp/dataflow-redis.conf
exec /usr/local/bin/docker-entrypoint.sh redis-server /tmp/dataflow-redis.conf
