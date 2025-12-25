"""
SLOP Hole Filler - LLM-based hole completion with tiered model routing

Routes holes to appropriately-sized models based on complexity,
with automatic escalation on verification failure.
"""

from dataclasses import dataclass
from typing import Optional, List, Dict, Any
from slop.parser import SExpr, SList, Symbol, String, parse, find_holes, is_form, pretty_print
from slop.providers import Tier, ModelConfig, Provider, MockProvider, create_default_configs


@dataclass
class Hole:
    type_expr: SExpr
    prompt: str
    complexity: Optional[str] = None
    constraints: Optional[List[SExpr]] = None
    examples: Optional[List[SExpr]] = None
    must_use: Optional[List[str]] = None


@dataclass
class FillResult:
    success: bool
    expression: Optional[SExpr] = None
    error: Optional[str] = None
    model_used: Optional[str] = None
    attempts: int = 0


def extract_hole(hole_expr: SList) -> Hole:
    """Extract hole information from AST"""
    items = hole_expr.items[1:]

    type_expr = items[0] if items else None
    prompt = items[1].value if len(items) > 1 and isinstance(items[1], String) else ""

    complexity = None
    constraints = None
    examples = None
    must_use = None

    i = 2
    while i < len(items):
        if isinstance(items[i], Symbol) and items[i].name.startswith(':'):
            key = items[i].name[1:]
            if i + 1 < len(items):
                val = items[i + 1]
                if key == 'complexity':
                    complexity = val.name if isinstance(val, Symbol) else str(val)
                elif key == 'constraints':
                    constraints = val.items if isinstance(val, SList) else [val]
                elif key == 'examples':
                    examples = val.items if isinstance(val, SList) else [val]
                elif key == 'must-use':
                    if isinstance(val, SList):
                        must_use = [str(x) for x in val.items]
                    else:
                        must_use = [str(val)]
                i += 2
            else:
                i += 1
        else:
            i += 1

    return Hole(type_expr, prompt, complexity, constraints, examples, must_use)


def classify_tier(hole: Hole) -> Tier:
    """Classify hole complexity"""

    # Explicit override
    if hole.complexity:
        tier_map = {
            'tier-1': Tier.TIER_1,
            'tier-2': Tier.TIER_2,
            'tier-3': Tier.TIER_3,
            'tier-4': Tier.TIER_4,
        }
        if hole.complexity in tier_map:
            return tier_map[hole.complexity]

    # Has examples → simpler
    base_tier = Tier.TIER_1 if hole.examples else Tier.TIER_2

    # Type complexity
    type_tier = _type_complexity(hole.type_expr)

    # Constraints complexity
    constraint_tier = Tier.TIER_1
    if hole.constraints and len(hole.constraints) > 2:
        constraint_tier = Tier.TIER_2

    return max([base_tier, type_tier, constraint_tier], key=lambda t: t.value)


def _type_complexity(type_expr: SExpr) -> Tier:
    """Determine complexity from type"""
    if isinstance(type_expr, Symbol):
        if type_expr.name in ['Bool', 'Int', 'String', 'Float', 'U8', 'I32']:
            return Tier.TIER_1
        return Tier.TIER_2

    if isinstance(type_expr, SList) and len(type_expr) > 0:
        head = type_expr[0]
        if isinstance(head, Symbol):
            if head.name == 'Fn':
                return Tier.TIER_3
            if head.name in ('Result', 'Option'):
                return Tier.TIER_2
            if head.name == 'List':
                return Tier.TIER_2

    return Tier.TIER_2


def build_prompt(hole: Hole, context: Dict[str, Any]) -> str:
    """Build prompt for hole filling"""
    sections = [
        "You are filling a typed hole in SLOP code. SLOP uses S-expression syntax.",
        "",
        "## Context",
        f"Function: {context.get('fn_name', 'unknown')}",
        f"Intent: {context.get('intent', '')}",
        f"Parameters: {context.get('params', '')}",
        f"Return type: {context.get('return_type', '')}",
        "",
        "## Hole to Fill",
        f"Type: {hole.type_expr}",
        f"Description: {hole.prompt}",
    ]

    if hole.constraints:
        sections.append("")
        sections.append("## Constraints")
        for c in hole.constraints:
            sections.append(f"- {c}")

    if hole.examples:
        sections.append("")
        sections.append("## Examples")
        for ex in hole.examples:
            sections.append(f"- {ex}")

    if hole.must_use:
        sections.append("")
        sections.append("## Must Use")
        sections.append(f"The expression MUST use: {', '.join(hole.must_use)}")

    sections.append("")
    sections.append("Respond with ONLY the SLOP S-expression. No explanation.")

    return '\n'.join(sections)


class HoleFiller:
    """Fill holes with tiered model routing"""

    def __init__(self, configs: Dict[Tier, ModelConfig], provider):
        self.configs = configs
        self.provider = provider
        self.max_retries = 2

    def fill(self, hole: Hole, context: Dict[str, Any]) -> FillResult:
        """Fill a single hole"""
        tier = classify_tier(hole)
        prompt = build_prompt(hole, context)

        attempts = 0
        current_tier = tier

        while current_tier.value <= Tier.TIER_4.value:
            config = self.configs.get(current_tier)
            if not config:
                # Skip to next tier if no config, but check bounds
                next_val = current_tier.value + 1
                if next_val > Tier.TIER_4.value:
                    break
                current_tier = Tier(next_val)
                continue

            for _ in range(self.max_retries):
                attempts += 1
                try:
                    response = self.provider.complete(prompt, config)
                    expr = self._parse_response(response)

                    if expr and self._validate(expr, hole):
                        return FillResult(
                            success=True,
                            expression=expr,
                            model_used=config.model,
                            attempts=attempts
                        )
                except Exception as e:
                    continue

            # Escalate to next tier, but check bounds
            next_val = current_tier.value + 1
            if next_val > Tier.TIER_4.value:
                break
            current_tier = Tier(next_val)

        return FillResult(
            success=False,
            error="Failed after all attempts",
            attempts=attempts
        )

    def _parse_response(self, response: str) -> Optional[SExpr]:
        """Parse LLM response"""
        response = response.strip()
        if response.startswith('```'):
            lines = response.split('\n')
            response = '\n'.join(lines[1:-1])

        try:
            exprs = parse(response)
            return exprs[0] if exprs else None
        except:
            return None

    def _validate(self, expr: SExpr, hole: Hole) -> bool:
        """Basic validation"""
        if hole.must_use:
            expr_str = str(expr)
            for must in hole.must_use:
                # Flexible matching for identifiers
                must_normalized = must.replace('-', '_').replace('.', '')
                expr_normalized = expr_str.replace('-', '_').replace('.', '')
                if must_normalized not in expr_normalized:
                    return False
        return True


def replace_hole_in_expr(expr: SExpr, hole: SList, replacement: SExpr) -> SExpr:
    """Replace a specific hole in an expression with its filled value"""
    if expr is hole:
        return replacement

    if isinstance(expr, SList):
        new_items = []
        for item in expr.items:
            new_items.append(replace_hole_in_expr(item, hole, replacement))
        return SList(new_items)

    return expr


def replace_holes_in_ast(ast: List[SExpr], replacements: Dict[int, SExpr]) -> List[SExpr]:
    """Replace holes in AST using id-based replacement map"""
    result = []
    for form in ast:
        new_form = _replace_holes_recursive(form, replacements)
        result.append(new_form)
    return result


def _replace_holes_recursive(expr: SExpr, replacements: Dict[int, SExpr]) -> SExpr:
    """Recursively replace holes identified by their id()"""
    if id(expr) in replacements:
        return replacements[id(expr)]

    if isinstance(expr, SList):
        new_items = []
        for item in expr.items:
            new_items.append(_replace_holes_recursive(item, replacements))
        return SList(new_items)

    return expr


if __name__ == '__main__':
    # Test
    test = '''
    (fn withdraw ((account (Ptr Account)) (amount (Int 1 ..)))
      (@intent "Withdraw funds")
      (hole (Result (Ptr Account) Error)
        "Check balance and deduct"
        :complexity tier-2
        :must-use (account amount)))
    '''

    from slop.parser import parse, find_holes
    ast = parse(test)

    holes = find_holes(ast[0])
    print(f"Found {len(holes)} holes")

    for h in holes:
        info = extract_hole(h)
        tier = classify_tier(info)
        print(f"  {info.prompt}: {tier.name}")
