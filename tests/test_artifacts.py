import json
from decimal import Decimal
from unittest.mock import Mock

from autocoder.artifacts import usage


def test_postgres_usage_is_json_serializable():
    session = Mock()
    session.scalar.return_value = Decimal("12345")
    assert json.loads(json.dumps({"cost": usage(session)})) == {"cost": 0.012345}
