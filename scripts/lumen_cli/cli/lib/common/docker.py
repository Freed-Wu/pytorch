"""
Docker Utility helpers for CLI tasks.
""""

import logging
from typing import Optional
import docker
from docker.errors import APIError, NotFound


logger = logging.getLogger(__name__)

# lazy singleton so we don't reconnect every call
_docker_client: Optional[docker.DockerClient] = None


def _get_client() -> docker.DockerClient:
    global _docker_client
    if _docker_client is None:
        _docker_client = docker.from_env()
    return _docker_client


def local_image_exists(
    image_name: str, client: Optional[docker.DockerClient] = None
) -> bool:
    """
    Return True if a local Docker image (by name:tag, id, or digest) exists.
    """
    client = client or _get_client()
    logger.info(f"Checking if image {image_name} exists...")
    try:
        client.images.get(image_name)
        logger.info(f"Found {image_name}...")
        return True
    except NotFound:
        logger.info(f"Image {image_name} not found locally...")
        return False
    except APIError as e:
        # Surface daemon/connection problems as False or re-raise — your choice.
        # Here we choose False to match the old "inspect fails => False" behavior.
        return False
