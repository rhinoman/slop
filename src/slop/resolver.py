"""
SLOP Module Resolver - Handles module discovery, dependency graphs, and linking.
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from slop.parser import (
    SExpr, SList, Symbol, parse_file, is_form,
    get_imports, get_exports, parse_import, parse_export,
    ImportSpec, ExportSpec
)


@dataclass
class ModuleInfo:
    """Information about a parsed module."""
    name: str
    path: Path
    ast: List[SExpr]
    exports: Dict[str, int] = field(default_factory=dict)  # name -> arity
    imports: List[ImportSpec] = field(default_factory=list)
    export_types: List[str] = field(default_factory=list)


@dataclass
class ModuleGraph:
    """Dependency graph of modules."""
    modules: Dict[str, ModuleInfo] = field(default_factory=dict)  # name -> info
    dependencies: Dict[str, List[str]] = field(default_factory=dict)  # name -> [dep_names]


class ResolverError(Exception):
    """Error during module resolution."""
    pass


class ModuleResolver:
    """Resolves module imports and builds dependency graphs."""

    def __init__(self, search_paths: List[Path] = None):
        """Initialize resolver with optional search paths.

        Args:
            search_paths: Directories to search for modules (in order).
                         Current directory of importing file is always searched first.
        """
        self.search_paths = search_paths or []
        self.cache: Dict[Path, ModuleInfo] = {}

    def resolve_module(self, module_name: str, from_path: Optional[Path] = None) -> Path:
        """Find the .slop file for a module name.

        Search order:
        1. Directory of the importing file (if from_path given)
        2. Configured search paths (in order)
        3. Current working directory

        Args:
            module_name: Name of module to find
            from_path: Path of the importing file (for relative resolution)

        Returns:
            Path to the .slop file

        Raises:
            ResolverError: If module cannot be found
        """
        filename = f"{module_name}.slop"
        search_dirs = []

        # Start with directory of importing file
        if from_path:
            search_dirs.append(from_path.parent)

        # Add configured search paths
        search_dirs.extend(self.search_paths)

        # Add current directory as fallback
        search_dirs.append(Path('.'))

        for dir_path in search_dirs:
            candidate = dir_path / filename
            if candidate.exists():
                return candidate.resolve()

        searched = ', '.join(str(p) for p in search_dirs)
        raise ResolverError(f"Module '{module_name}' not found (searched: {searched})")

    def load_module(self, path: Path) -> ModuleInfo:
        """Parse a .slop file and extract module information.

        Args:
            path: Path to .slop file

        Returns:
            ModuleInfo with parsed AST and extracted exports/imports
        """
        path = path.resolve()

        # Check cache
        if path in self.cache:
            return self.cache[path]

        # Parse file
        ast = parse_file(str(path))

        # Find module form
        module_name = path.stem  # Default to filename
        module_forms = ast

        for form in ast:
            if is_form(form, 'module'):
                if len(form) > 1 and isinstance(form[1], Symbol):
                    module_name = form[1].name
                # Extract forms from inside module
                module_forms = list(form.items[2:])
                break

        # Extract exports
        exports = {}
        for exp_form in get_exports(module_forms):
            spec = parse_export(exp_form)
            for name, arity in spec.symbols:
                exports[name] = arity

        # Extract imports
        imports = []
        for imp_form in get_imports(module_forms):
            imports.append(parse_import(imp_form))

        info = ModuleInfo(
            name=module_name,
            path=path,
            ast=ast,
            exports=exports,
            imports=imports
        )

        self.cache[path] = info
        return info

    def build_dependency_graph(self, entry_path: Path) -> ModuleGraph:
        """Build complete dependency graph starting from entry module.

        Args:
            entry_path: Path to the entry point .slop file

        Returns:
            ModuleGraph with all modules and their dependencies
        """
        graph = ModuleGraph()
        to_process = [entry_path.resolve()]
        processed = set()

        while to_process:
            path = to_process.pop(0)
            if path in processed:
                continue
            processed.add(path)

            # Load module
            info = self.load_module(path)
            graph.modules[info.name] = info
            graph.dependencies[info.name] = []

            # Process imports
            for imp in info.imports:
                graph.dependencies[info.name].append(imp.module_name)

                # Resolve and queue import
                try:
                    dep_path = self.resolve_module(imp.module_name, path)
                    if dep_path not in processed:
                        to_process.append(dep_path)
                except ResolverError:
                    # Will be caught during validation
                    pass

        return graph

    def detect_cycles(self, graph: ModuleGraph) -> Optional[List[str]]:
        """Detect circular dependencies in module graph.

        Args:
            graph: ModuleGraph to check

        Returns:
            List of module names forming a cycle, or None if no cycles
        """
        WHITE, GRAY, BLACK = 0, 1, 2
        color = {name: WHITE for name in graph.modules}
        parent = {}

        def dfs(node: str) -> Optional[List[str]]:
            color[node] = GRAY

            for dep in graph.dependencies.get(node, []):
                if dep not in color:
                    # Module not found - skip, will error during validation
                    continue

                if color[dep] == GRAY:
                    # Found cycle - reconstruct path
                    cycle = [dep, node]
                    current = node
                    while parent.get(current) != dep and current in parent:
                        current = parent[current]
                        cycle.append(current)
                    return cycle[::-1]

                if color[dep] == WHITE:
                    parent[dep] = node
                    result = dfs(dep)
                    if result:
                        return result

            color[node] = BLACK
            return None

        for node in graph.modules:
            if color[node] == WHITE:
                result = dfs(node)
                if result:
                    return result

        return None

    def topological_sort(self, graph: ModuleGraph) -> List[str]:
        """Sort modules in dependency order (dependencies first).

        Args:
            graph: ModuleGraph to sort

        Returns:
            List of module names in topological order

        Raises:
            ResolverError: If circular dependency detected
        """
        cycle = self.detect_cycles(graph)
        if cycle:
            cycle_str = ' -> '.join(cycle + [cycle[0]])
            raise ResolverError(f"Circular dependency: {cycle_str}")

        # Kahn's algorithm
        in_degree = {name: 0 for name in graph.modules}
        for deps in graph.dependencies.values():
            for dep in deps:
                if dep in in_degree:
                    in_degree[dep] += 1

        # Start with modules that have no dependents
        queue = [name for name, degree in in_degree.items() if degree == 0]
        result = []

        while queue:
            node = queue.pop(0)
            result.append(node)

            for dep in graph.dependencies.get(node, []):
                if dep in in_degree:
                    in_degree[dep] -= 1
                    if in_degree[dep] == 0:
                        queue.append(dep)

        # Reverse to get dependencies first
        return result[::-1]

    def validate_imports(self, graph: ModuleGraph) -> List[str]:
        """Validate that all imports are satisfied by exports.

        Args:
            graph: ModuleGraph to validate

        Returns:
            List of error messages (empty if valid)
        """
        errors = []

        for module_name, info in graph.modules.items():
            for imp in info.imports:
                # Check module exists
                if imp.module_name not in graph.modules:
                    errors.append(
                        f"{info.path}:{imp.line}: Module '{imp.module_name}' not found"
                    )
                    continue

                source = graph.modules[imp.module_name]

                # Check each imported symbol
                for name, arity in imp.symbols:
                    if name not in source.exports:
                        errors.append(
                            f"{info.path}:{imp.line}: '{name}' not exported from '{imp.module_name}'"
                        )
                    elif source.exports[name] != arity:
                        errors.append(
                            f"{info.path}:{imp.line}: '{name}' has arity {source.exports[name]}, not {arity}"
                        )

        return errors


def resolve_modules(entry_path: str, search_paths: List[str] = None) -> Tuple[ModuleGraph, List[str]]:
    """Convenience function to resolve modules from entry point.

    Args:
        entry_path: Path to entry .slop file
        search_paths: Additional directories to search

    Returns:
        Tuple of (ModuleGraph, build_order) where build_order is topologically sorted
    """
    resolver = ModuleResolver([Path(p) for p in (search_paths or [])])
    graph = resolver.build_dependency_graph(Path(entry_path))

    # Check for cycles
    cycle = resolver.detect_cycles(graph)
    if cycle:
        cycle_str = ' -> '.join(cycle + [cycle[0]])
        raise ResolverError(f"Circular dependency: {cycle_str}")

    # Get build order
    order = resolver.topological_sort(graph)

    return graph, order
