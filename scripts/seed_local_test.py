"""Retired v1 pilot helper; operator-owned repositories are required in v2."""


def main():
    raise SystemExit(
        "This v1 pilot helper is disabled. Configure the v2 example with a dedicated bot "
        "credential, and have the operator initialize the repository. See docs/DEPLOYMENT.md."
    )


if __name__ == "__main__":
    main()
