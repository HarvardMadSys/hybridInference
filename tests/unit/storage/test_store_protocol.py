"""Protocol conformance tests for store implementations.

Verifies that PostgresOperationalStore and PostgresLogStore implement
every abstract method defined by their respective ABCs. No database
connection is needed — these are purely structural checks.
"""

import inspect

from serving.storage.base import LogStore, OperationalStore
from serving.storage.cache import CachedOperationalStore
from serving.storage.dual_write import DualWriteLogStore, DualWriteOperationalStore
from serving.storage.postgres_log import PostgresLogStore
from serving.storage.postgres_operational import PostgresOperationalStore


def _get_abstract_methods(abc_class: type) -> set[str]:
    """Return the set of abstract method names on an ABC."""
    return {
        name
        for name, _ in inspect.getmembers(abc_class, predicate=inspect.isfunction)
        if getattr(getattr(abc_class, name), "__isabstractmethod__", False)
    }


class TestPostgresOperationalStoreProtocol:
    """Verify PostgresOperationalStore satisfies OperationalStore."""

    def test_implements_all_abstract_methods(self):
        """Every OperationalStore abstract method has a concrete override."""
        required = _get_abstract_methods(OperationalStore)
        implemented = set(dir(PostgresOperationalStore))

        missing = required - implemented
        assert not missing, f"PostgresOperationalStore is missing methods: {sorted(missing)}"

    def test_no_abstractmethod_flag_remains(self):
        """The concrete class should have zero unresolved abstract methods."""
        remaining = getattr(PostgresOperationalStore, "__abstractmethods__", set())
        assert not remaining, f"Unresolved abstract methods: {sorted(remaining)}"

    def test_method_signatures_match(self):
        """Concrete method signatures must accept the same parameters as the ABC."""
        for name in _get_abstract_methods(OperationalStore):
            abc_sig = inspect.signature(getattr(OperationalStore, name))
            impl_sig = inspect.signature(getattr(PostgresOperationalStore, name))

            abc_params = set(abc_sig.parameters.keys())
            impl_params = set(impl_sig.parameters.keys())

            assert abc_params == impl_params, (
                f"{name}: signature mismatch — "
                f"ABC has {sorted(abc_params)}, impl has {sorted(impl_params)}"
            )


class TestCachedOperationalStoreProtocol:
    """Verify CachedOperationalStore satisfies OperationalStore."""

    def test_implements_all_abstract_methods(self):
        """Every OperationalStore abstract method has a concrete override."""
        required = _get_abstract_methods(OperationalStore)
        implemented = set(dir(CachedOperationalStore))

        missing = required - implemented
        assert not missing, f"CachedOperationalStore is missing methods: {sorted(missing)}"

    def test_no_abstractmethod_flag_remains(self):
        """The concrete class should have zero unresolved abstract methods."""
        remaining = getattr(CachedOperationalStore, "__abstractmethods__", set())
        assert not remaining, f"Unresolved abstract methods: {sorted(remaining)}"

    def test_method_signatures_match(self):
        """Concrete method signatures must accept the same parameters as the ABC."""
        for name in _get_abstract_methods(OperationalStore):
            abc_sig = inspect.signature(getattr(OperationalStore, name))
            impl_sig = inspect.signature(getattr(CachedOperationalStore, name))

            abc_params = set(abc_sig.parameters.keys())
            impl_params = set(impl_sig.parameters.keys())

            assert abc_params == impl_params, (
                f"{name}: signature mismatch — "
                f"ABC has {sorted(abc_params)}, impl has {sorted(impl_params)}"
            )


class TestPostgresLogStoreProtocol:
    """Verify PostgresLogStore satisfies LogStore."""

    def test_implements_all_abstract_methods(self):
        """Every LogStore abstract method has a concrete override."""
        required = _get_abstract_methods(LogStore)
        implemented = set(dir(PostgresLogStore))

        missing = required - implemented
        assert not missing, f"PostgresLogStore is missing methods: {sorted(missing)}"

    def test_no_abstractmethod_flag_remains(self):
        """The concrete class should have zero unresolved abstract methods."""
        remaining = getattr(PostgresLogStore, "__abstractmethods__", set())
        assert not remaining, f"Unresolved abstract methods: {sorted(remaining)}"

    def test_method_signatures_match(self):
        """Concrete method signatures must accept the same parameters as the ABC."""
        for name in _get_abstract_methods(LogStore):
            abc_sig = inspect.signature(getattr(LogStore, name))
            impl_sig = inspect.signature(getattr(PostgresLogStore, name))

            abc_params = set(abc_sig.parameters.keys())
            impl_params = set(impl_sig.parameters.keys())

            assert abc_params == impl_params, (
                f"{name}: signature mismatch — "
                f"ABC has {sorted(abc_params)}, impl has {sorted(impl_params)}"
            )


class TestDualWriteOperationalStoreProtocol:
    """Verify DualWriteOperationalStore satisfies OperationalStore."""

    def test_implements_all_abstract_methods(self):
        required = _get_abstract_methods(OperationalStore)
        implemented = set(dir(DualWriteOperationalStore))
        missing = required - implemented
        assert not missing, f"DualWriteOperationalStore is missing methods: {sorted(missing)}"

    def test_no_abstractmethod_flag_remains(self):
        remaining = getattr(DualWriteOperationalStore, "__abstractmethods__", set())
        assert not remaining, f"Unresolved abstract methods: {sorted(remaining)}"

    def test_method_signatures_match(self):
        for name in _get_abstract_methods(OperationalStore):
            abc_sig = inspect.signature(getattr(OperationalStore, name))
            impl_sig = inspect.signature(getattr(DualWriteOperationalStore, name))
            abc_params = set(abc_sig.parameters.keys())
            impl_params = set(impl_sig.parameters.keys())
            assert abc_params == impl_params, (
                f"{name}: signature mismatch — "
                f"ABC has {sorted(abc_params)}, impl has {sorted(impl_params)}"
            )


class TestDualWriteLogStoreProtocol:
    """Verify DualWriteLogStore satisfies LogStore."""

    def test_implements_all_abstract_methods(self):
        required = _get_abstract_methods(LogStore)
        implemented = set(dir(DualWriteLogStore))
        missing = required - implemented
        assert not missing, f"DualWriteLogStore is missing methods: {sorted(missing)}"

    def test_no_abstractmethod_flag_remains(self):
        remaining = getattr(DualWriteLogStore, "__abstractmethods__", set())
        assert not remaining, f"Unresolved abstract methods: {sorted(remaining)}"

    def test_method_signatures_match(self):
        for name in _get_abstract_methods(LogStore):
            abc_sig = inspect.signature(getattr(LogStore, name))
            impl_sig = inspect.signature(getattr(DualWriteLogStore, name))
            abc_params = set(abc_sig.parameters.keys())
            impl_params = set(impl_sig.parameters.keys())
            assert abc_params == impl_params, (
                f"{name}: signature mismatch — "
                f"ABC has {sorted(abc_params)}, impl has {sorted(impl_params)}"
            )
