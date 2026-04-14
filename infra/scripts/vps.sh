#!/usr/bin/env bash
# Manage the SaasMint VPS Docker Compose stack.
# Usage: vps.sh {up|down|restart|logs|exec|ps}
set -euo pipefail

COMPOSE_FILE="$(dirname "$0")/../docker-compose.vps.yml"

cmd="${1:-help}"
shift || true

case "$cmd" in
    up)
        docker compose -f "$COMPOSE_FILE" up -d --build "$@"
        ;;
    down)
        docker compose -f "$COMPOSE_FILE" down "$@"
        ;;
    restart)
        docker compose -f "$COMPOSE_FILE" restart "$@"
        ;;
    logs)
        docker compose -f "$COMPOSE_FILE" logs -f "$@"
        ;;
    exec)
        docker compose -f "$COMPOSE_FILE" exec "$@"
        ;;
    ps)
        docker compose -f "$COMPOSE_FILE" ps "$@"
        ;;
    *)
        echo "Usage: vps.sh {up|down|restart|logs|exec|ps}"
        echo ""
        echo "  up        Build and start all services"
        echo "  down      Stop and remove all services"
        echo "  restart   Restart services (e.g. vps.sh restart django)"
        echo "  logs      Tail logs (e.g. vps.sh logs django)"
        echo "  exec      Run a command (e.g. vps.sh exec django bash)"
        echo "  ps        List running services"
        exit 1
        ;;
esac
