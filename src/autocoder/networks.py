from docker.errors import NotFound


def require_managed(obj):
    labels = getattr(obj, "labels", None) or obj.attrs.get("Labels", {})
    if labels.get("autocoder.managed") != "true":
        raise ValueError("Refusing operation on an unlabeled Docker object")


def create_attempt_network(client, attempt_id):
    return client.networks.create("att-" + attempt_id, internal=True,
        labels={"autocoder.managed": "true", "autocoder.attempt": attempt_id})


def attach(network, container, **kwargs):
    require_managed(container)
    network.connect(container, **kwargs)


def detach(network, container):
    require_managed(container)
    network.disconnect(container, force=True)


def remove_network(network):
    require_managed(network)
    try:
        network.remove()
    except NotFound:
        pass


def isolation_probe(runner):
    script = ("import socket,sys; results=[]\n"
              "for host in ['github.com','openrouter.ai','1.1.1.1']:\n"
              " try:\n  s=socket.create_connection((host,443),timeout=2);s.close();results.append(host)\n"
              " except OSError: pass\n"
              "sys.exit(1 if results else 0)")
    if runner.command(["python3", "-c", script], timeout=12)["exit_code"]:
        raise RuntimeError("Builder egress isolation probe failed")
