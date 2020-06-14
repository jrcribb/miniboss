import time
import logging
from collections import Counter, Mapping
import threading
import copy

import click
import requests
import furl
import requests.exceptions

from drillmaster.docker_client import get_client
from drillmaster.service_agent import ServiceAgent

logging.basicConfig(
    level=logging.INFO,
    style='{',
    format= '[%(asctime)s] %(pathname)s:%(lineno)d %s(levelname)s - %(message)s'
)

logger = logging.getLogger(__name__)

KEYCLOAK_PORT = 8090
OSTKREUZ_PORT = 8080


class ServiceLoadError(Exception):
    pass

class ServiceDefinitionError(Exception):
    pass

class ServiceMeta(type):
    def __new__(cls, name, bases, attrdict):
        if not bases:
            return super().__new__(cls, name, bases, attrdict)
        if not isinstance(attrdict.get("name"), str) or attrdict["name"] == "":
            raise ServiceDefinitionError(
                "Field 'name' of service class {:s} must be a non-empty string".format(name))
        if not isinstance(attrdict.get("image"), str) or attrdict["image"] == "":
            raise ServiceDefinitionError(
                "Field 'image' of service class {:s} must be a non-empty string".format(name))
        if "ports" in attrdict and not isinstance(attrdict["ports"], Mapping):
            raise ServiceDefinitionError(
                "Field 'ports' of service class {:s} must be a mapping".format(name))
        if "env" in attrdict and not isinstance(attrdict["env"], Mapping):
            raise ServiceDefinitionError(
                "Field 'env' of service class {:s} must be a mapping".format(name))
        return super().__new__(cls, name, bases, attrdict)


class Service(metaclass=ServiceMeta):
    name = None
    dependencies = []
    ports = {}
    env = {}

    def ping(self):
        return True

    def post_init(self):
        pass

class RunningContext:

    def __init__(self, services_by_name, network_name, collection, timeout):
        self.service_agents = {name: ServiceAgent(service, network_name, collection, timeout)
                               for name, service in services_by_name.items()}
        self.without_dependencies = [x for x in self.service_agents.values() if x.can_start]
        self.waiting_agents = {name: agent for name, agent in self.service_agents.items()
                               if not agent.can_start}

    @property
    def done(self):
        return not bool(self.waiting_agents)

    def service_started(self, started_service):
        self.service_agents.pop(started_service)
        startable = []
        for name, agent in self.waiting_agents.items():
            agent.process_service_started(started_service)
            if agent.can_start:
                startable.append(name)
        return [self.waiting_agents.pop(name) for name in startable]


class ServiceCollection:

    def __init__(self):
        self.all_by_name = {}
        self._base_class = Service
        self.running_context = None
        self.service_pop_lock = threading.Lock()
        self.failed = False

    def load_definitions(self, exclude=None):
        exclude = exclude or []
        services = self._base_class.__subclasses__()
        if len(services) == 0:
            raise ServiceLoadError("No services defined")
        name_counter = Counter()
        for service in services:
            if service.name not in exclude:
                self.all_by_name[service.name] = service
                excluded_deps = [dep for dep in service.dependencies if dep in exclude]
                if excluded_deps:
                    raise ServiceLoadError("{:s} is to be excluded, but {:s} depends on it".format(
                        excluded_deps[0], service.name))
            name_counter[service.name] += 1
        multiples = [name for name,count in name_counter.items() if count > 1]
        if multiples:
            raise ServiceLoadError("Repeated service names: {:s}".format(",".join(multiples)))
        for service in self.all_by_name.values():
            dependencies = service.dependencies[:]
            service.dependencies = [self.all_by_name[dependency] for dependency in dependencies]
        self.check_circular_dependencies()

    def check_circular_dependencies(self):
        with_dependencies = [x for x in self.all_by_name.values() if x.dependencies != []]
        for service in with_dependencies:
            start = service.name
            count = 0
            def go_up_dependencies(checked):
                nonlocal count
                count += 1
                for dependency in checked.dependencies:
                    if dependency.name == start:
                        raise ServiceLoadError("Circular dependency detected")
                    if count == len(self.all_by_name):
                        return
                    go_up_dependencies(dependency)
            go_up_dependencies(service)

    def __len__(self):
        return len(self.all_by_name)

    def start_next(self, started_service):
        with self.service_pop_lock:
            new_startables = self.running_context.service_started(started_service)
            for agent in new_startables:
                agent.start()

    def service_failed(self, failed_service):
        self.failed = True

    def start_all(self, create_new, network_name, timeout):
        self.running_context = RunningContext(self.all_by_name, network_name, self, timeout)
        for agent in self.running_context.without_dependencies:
            agent.start()
        while not (self.running_context.done or self.failed):
            time.sleep(0.05)
        if self.failed:
            logger.error("Failed to start all services")


def start_services(create_new, exclude, network_name, timeout):
    docker = get_client()
    collection = ServiceCollection()
    collection.load_definitions(exclude=exclude)
    existing_network = docker.networks.list(names=[network_name])
    if not existing_network:
        network = docker.networks.create(network_name, driver="bridge")
        logger.info("Created network %s", network_name)
    service_names = collection.start_all(create_new, network_name, timeout)
    logger.info("Started services: %s", ",".join(service_names))
