"""
SLOP Type System - Shared type definitions and constants

This module contains the type classes and constants shared across SLOP tools.
Tools should import from here rather than from each other to avoid coupling.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Union, List, Dict, Optional, Tuple, Any, Set


# ============================================================================
# Range Bounds
# ============================================================================

@dataclass(frozen=True)
class RangeBounds:
    """Represents numeric range bounds [min, max]"""
    min_val: Optional[int] = None  # None = unbounded below
    max_val: Optional[int] = None  # None = unbounded above

    def __str__(self) -> str:
        if self.min_val is None and self.max_val is None:
            return ".."
        elif self.min_val is None:
            return f".. {self.max_val}"
        elif self.max_val is None:
            return f"{self.min_val} .."
        else:
            return f"{self.min_val} .. {self.max_val}"

    def union(self, other: 'RangeBounds') -> 'RangeBounds':
        """Union of two ranges (widens bounds)"""
        new_min = None
        new_max = None
        if self.min_val is not None and other.min_val is not None:
            new_min = min(self.min_val, other.min_val)
        if self.max_val is not None and other.max_val is not None:
            new_max = max(self.max_val, other.max_val)
        return RangeBounds(new_min, new_max)

    def intersect(self, other: 'RangeBounds') -> 'RangeBounds':
        """Intersection of two ranges (tightens bounds)"""
        new_min = self.min_val
        new_max = self.max_val
        if other.min_val is not None:
            if new_min is None or other.min_val > new_min:
                new_min = other.min_val
        if other.max_val is not None:
            if new_max is None or other.max_val < new_max:
                new_max = other.max_val
        return RangeBounds(new_min, new_max)

    def contains(self, value: int) -> bool:
        if self.min_val is not None and value < self.min_val:
            return False
        if self.max_val is not None and value > self.max_val:
            return False
        return True

    def is_subrange_of(self, other: 'RangeBounds') -> bool:
        """Check if this range is contained within other"""
        if other.min_val is not None:
            if self.min_val is None or self.min_val < other.min_val:
                return False
        if other.max_val is not None:
            if self.max_val is None or self.max_val > other.max_val:
                return False
        return True

    def is_empty(self) -> bool:
        """Check if range has no valid values"""
        if self.min_val is not None and self.max_val is not None:
            return self.min_val > self.max_val
        return False


# ============================================================================
# Path-Sensitive Constraints
# ============================================================================

@dataclass
class Constraint:
    """Type refinement constraint extracted from conditions.

    Example: (> x 0) produces Constraint(var='x', op='>', value=0)
    """
    var: str           # Variable name (or field path like 'limiter.tokens')
    op: str            # '>', '<', '>=', '<=', '==', '!='
    value: int         # Comparison value

    def negate(self) -> 'Constraint':
        """Return the negated constraint for else-branch."""
        negations = {
            '>': '<=',
            '<': '>=',
            '>=': '<',
            '<=': '>',
            '==': '!=',
            '!=': '==',
        }
        return Constraint(self.var, negations[self.op], self.value)

    def to_bounds(self) -> RangeBounds:
        """Convert constraint to range bounds for intersection."""
        if self.op == '>':
            return RangeBounds(self.value + 1, None)
        elif self.op == '>=':
            return RangeBounds(self.value, None)
        elif self.op == '<':
            return RangeBounds(None, self.value - 1)
        elif self.op == '<=':
            return RangeBounds(None, self.value)
        elif self.op == '==':
            return RangeBounds(self.value, self.value)
        elif self.op == '!=':
            # Can't represent != as a single range
            return RangeBounds()
        return RangeBounds()


# ============================================================================
# Type Classes
# ============================================================================

class Type(ABC):
    """Abstract base for all types"""

    @abstractmethod
    def __str__(self) -> str:
        pass

    @abstractmethod
    def equals(self, other: 'Type') -> bool:
        pass

    def is_subtype_of(self, other: 'Type') -> bool:
        """Default: types must be equal"""
        return self.equals(other)


@dataclass(frozen=True)
class PrimitiveType(Type):
    """Primitive types: Int, Float, Bool, String, Unit, etc."""
    name: str

    def __str__(self) -> str:
        return self.name

    def equals(self, other: Type) -> bool:
        return isinstance(other, PrimitiveType) and self.name == other.name

    def is_subtype_of(self, other: Type) -> bool:
        if self.equals(other):
            return True
        # Int subtypes: I8 <: I16 <: I32 <: I64 <: Int
        int_order = ['I8', 'I16', 'I32', 'I64', 'Int']
        uint_order = ['U8', 'U16', 'U32', 'U64']
        if isinstance(other, PrimitiveType):
            if self.name in int_order and other.name in int_order:
                return int_order.index(self.name) <= int_order.index(other.name)
            if self.name in uint_order and other.name in uint_order:
                return uint_order.index(self.name) <= uint_order.index(other.name)
        return False


@dataclass(frozen=True)
class RangeType(Type):
    """Integer type with range bounds: (Int 0 .. 100)"""
    base: str  # 'Int', 'I8', 'U8', etc.
    bounds: RangeBounds

    def __str__(self) -> str:
        if self.bounds.min_val is None and self.bounds.max_val is None:
            return f"({self.base})"
        return f"({self.base} {self.bounds})"

    def equals(self, other: Type) -> bool:
        if isinstance(other, RangeType):
            return self.base == other.base and self.bounds == other.bounds
        return False

    def is_subtype_of(self, other: Type) -> bool:
        if self.equals(other):
            return True
        if isinstance(other, RangeType) and self.base == other.base:
            return self.bounds.is_subrange_of(other.bounds)
        if isinstance(other, PrimitiveType) and other.name == self.base:
            return True
        return False


@dataclass(frozen=True)
class ListType(Type):
    """List type: (List T) or (List T min .. max)"""
    element_type: Type
    length_bounds: Optional[RangeBounds] = None

    def __str__(self) -> str:
        if self.length_bounds is None:
            return f"(List {self.element_type})"
        return f"(List {self.element_type} {self.length_bounds})"

    def equals(self, other: Type) -> bool:
        if isinstance(other, ListType):
            return self.element_type.equals(other.element_type)
        return False

    def is_subtype_of(self, other: Type) -> bool:
        """List is covariant: List<A> <: List<B> if A <: B.

        Special case: UnknownType in element position matches any type.
        This allows (List ?) from list-new to unify with (List T).
        """
        if self.equals(other):
            return True
        if isinstance(other, ListType):
            # Allow UnknownType to match any element type
            if isinstance(self.element_type, UnknownType) or isinstance(other.element_type, UnknownType):
                return True
            return self.element_type.is_subtype_of(other.element_type)
        return False


@dataclass(frozen=True)
class ArrayType(Type):
    """Fixed-size array: (Array T n)"""
    element_type: Type
    size: int

    def __str__(self) -> str:
        return f"(Array {self.element_type} {self.size})"

    def equals(self, other: Type) -> bool:
        if isinstance(other, ArrayType):
            return (self.element_type.equals(other.element_type) and
                    self.size == other.size)
        return False


@dataclass(frozen=True)
class MapType(Type):
    """Map type: (Map K V)"""
    key_type: Type
    value_type: Type

    def __str__(self) -> str:
        return f"(Map {self.key_type} {self.value_type})"

    def equals(self, other: Type) -> bool:
        if isinstance(other, MapType):
            return self.key_type.equals(other.key_type) and self.value_type.equals(other.value_type)
        return False

    def is_subtype_of(self, other: Type) -> bool:
        """Map is covariant in both key and value types.

        Special case: UnknownType in key or value position matches any type.
        """
        if self.equals(other):
            return True
        if isinstance(other, MapType):
            if (isinstance(self.key_type, UnknownType) or isinstance(other.key_type, UnknownType) or
                isinstance(self.value_type, UnknownType) or isinstance(other.value_type, UnknownType)):
                return True
            return (self.key_type.is_subtype_of(other.key_type) and
                    self.value_type.is_subtype_of(other.value_type))
        return False


@dataclass
class RecordType(Type):
    """Record/struct type: (record (field1 T1) ...)"""
    name: str
    fields: Dict[str, Type]

    def __str__(self) -> str:
        return self.name

    def equals(self, other: Type) -> bool:
        # Match RecordType by name
        if isinstance(other, RecordType):
            return self.name == other.name
        # Also match PrimitiveType by name (for String/Bytes backwards compat)
        if isinstance(other, PrimitiveType):
            return self.name == other.name
        return False

    def get_field(self, name: str) -> Optional[Type]:
        return self.fields.get(name)


@dataclass
class EnumType(Type):
    """Enum type: (enum val1 val2 ...)"""
    name: str
    variants: List[str]

    def __str__(self) -> str:
        return self.name

    def equals(self, other: Type) -> bool:
        return isinstance(other, EnumType) and self.name == other.name

    def has_variant(self, name: str) -> bool:
        return name in self.variants


@dataclass
class UnionType(Type):
    """Tagged union: (union (tag1 T1) (tag2) ...)"""
    name: str
    variants: Dict[str, Optional[Type]]  # tag -> payload type (None if no payload)

    def __str__(self) -> str:
        return self.name

    def equals(self, other: Type) -> bool:
        return isinstance(other, UnionType) and self.name == other.name

    def get_variant(self, tag: str) -> Optional[Type]:
        return self.variants.get(tag)


@dataclass(frozen=True)
class OptionType(Type):
    """Option type: (Option T) = (union (some T) (none))"""
    inner: Type

    def __str__(self) -> str:
        return f"(Option {self.inner})"

    def equals(self, other: Type) -> bool:
        if isinstance(other, OptionType):
            return self.inner.equals(other.inner)
        return False

    def is_subtype_of(self, other: Type) -> bool:
        """Option is covariant: Option<Subtype> <: Option<Supertype>"""
        if self.equals(other):
            return True
        if isinstance(other, OptionType):
            return self.inner.is_subtype_of(other.inner)
        return False


@dataclass(frozen=True)
class ResultType(Type):
    """Result type: (Result T E)"""
    ok_type: Type
    err_type: Type

    def __str__(self) -> str:
        return f"(Result {self.ok_type} {self.err_type})"

    def equals(self, other: Type) -> bool:
        if isinstance(other, ResultType):
            return (self.ok_type.equals(other.ok_type) and
                    self.err_type.equals(other.err_type))
        return False

    def is_subtype_of(self, other: Type) -> bool:
        """Result subtyping with TypeVar handling.

        (Result T TypeVar) is subtype of (Result T E) for any E.
        This allows (ok x) to match any expected Result error type.
        """
        if self.equals(other):
            return True
        if isinstance(other, ResultType):
            # ok_type must be subtype
            if not self.ok_type.is_subtype_of(other.ok_type):
                return False
            # err_type: if self has TypeVar, it matches anything
            if isinstance(self.err_type, TypeVar) and not self.err_type.bound:
                return True
            # Otherwise, err_type must be subtype
            return self.err_type.is_subtype_of(other.err_type)
        return False


@dataclass(frozen=True)
class PtrType(Type):
    """Pointer type: (Ptr T), (OwnPtr T), (OptPtr T)"""
    pointee: Type
    nullable: bool = False
    owning: bool = False

    def __str__(self) -> str:
        if self.nullable:
            return f"(OptPtr {self.pointee})"
        if self.owning:
            return f"(OwnPtr {self.pointee})"
        return f"(Ptr {self.pointee})"

    def equals(self, other: Type) -> bool:
        if isinstance(other, PtrType):
            return (self.pointee.equals(other.pointee) and
                    self.nullable == other.nullable and
                    self.owning == other.owning)
        return False


@dataclass(frozen=True)
class FnType(Type):
    """Function type: (Fn (A B) -> R)"""
    param_types: Tuple[Type, ...]
    return_type: Type

    def __str__(self) -> str:
        params = ' '.join(str(p) for p in self.param_types)
        return f"(Fn ({params}) -> {self.return_type})"

    def equals(self, other: Type) -> bool:
        if not isinstance(other, FnType):
            return False
        if len(self.param_types) != len(other.param_types):
            return False
        return (all(p.equals(q) for p, q in zip(self.param_types, other.param_types)) and
                self.return_type.equals(other.return_type))


@dataclass
class TypeVar(Type):
    """Type variable for inference"""
    id: int
    name: Optional[str] = None
    bound: Optional[Type] = None  # Resolved type

    def __str__(self) -> str:
        if self.bound:
            return str(self.bound)
        return self.name or f"?{self.id}"

    def equals(self, other: Type) -> bool:
        if self.bound:
            return self.bound.equals(other)
        return isinstance(other, TypeVar) and self.id == other.id

    def is_subtype_of(self, other: Type) -> bool:
        """TypeVar subtyping: bound TypeVar delegates, unbound matches anything"""
        if self.bound:
            return self.bound.is_subtype_of(other)
        # Unbound TypeVar is subtype of anything (will be unified later)
        return True

    def resolve(self) -> Type:
        """Follow type variable chain to get concrete type"""
        if self.bound:
            if isinstance(self.bound, TypeVar):
                return self.bound.resolve()
            return self.bound
        return self


# Singleton for unknown/error types
class UnknownType(Type):
    """Placeholder for unknown types during inference"""
    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __str__(self) -> str:
        return "?"

    def equals(self, other: Type) -> bool:
        return isinstance(other, UnknownType)


# ============================================================================
# Type Constants (single source of truth)
# ============================================================================

UNKNOWN = UnknownType()

# Common primitive type constants
STRING = PrimitiveType('String')
INT = PrimitiveType('Int')
BOOL = PrimitiveType('Bool')
UNIT = PrimitiveType('Unit')
ARENA = PrimitiveType('Arena')


# ============================================================================
# Built-in Function Names (single source of truth)
# ============================================================================

BUILTIN_FUNCTIONS = {
    # I/O
    'print', 'println',
    # String operations
    'string-len', 'string-concat', 'string-eq', 'string-new', 'string-slice',
    'string-split', 'int-to-string',
    # Arena/memory operations
    'arena-new', 'arena-alloc', 'arena-free',
    # List operations
    'list-new', 'list-push', 'list-get', 'list-len',
    # Map operations
    'map-new', 'map-put', 'map-get', 'map-has', 'map-empty',
    # Result operations
    'is-ok', 'unwrap',
    # Time
    'now-ms', 'sleep-ms',
    # Math
    'min', 'max',
}
