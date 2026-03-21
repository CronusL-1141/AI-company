"""Skill Registry — Progressive skill discovery for AI Team OS.

Provides a 3-layer progressive loading system:
  Layer 1 (quick):    Top 3-5 recommendations based on task description
  Layer 2 (category): Browse skills grouped by task category
  Layer 3 (full):     Complete documentation for a single skill

Skills are defined statically here rather than parsed from markdown,
ensuring fast lookups and type safety.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Skill:
    """A single ecosystem skill/plugin entry."""

    id: str
    name: str
    oneliner: str  # one-sentence description for Layer 1
    category: str  # grouping key for Layer 2
    install_cmd: str
    tags: list[str] = field(default_factory=list)
    github: str = ""
    stars: str = ""
    features: list[str] = field(default_factory=list)
    os_complement: str = ""  # how it complements AI Team OS
    use_cases: list[str] = field(default_factory=list)
    variants: list[str] = field(default_factory=list)
    compatibility: str = ""

    # ---- Layer output helpers ----

    def to_layer1(self) -> dict[str, Any]:
        """Quick recommendation format — name + oneliner + install."""
        return {
            "id": self.id,
            "name": self.name,
            "oneliner": self.oneliner,
            "category": self.category,
            "install_cmd": self.install_cmd,
        }

    def to_layer2(self) -> dict[str, Any]:
        """Category browsing format — adds tags, features, use_cases."""
        data = self.to_layer1()
        data.update({
            "tags": self.tags,
            "github": self.github,
            "stars": self.stars,
            "features": self.features,
            "use_cases": self.use_cases,
        })
        return data

    def to_layer3(self) -> dict[str, Any]:
        """Full detail format — everything we know about the skill."""
        data = self.to_layer2()
        data.update({
            "os_complement": self.os_complement,
            "variants": self.variants,
            "compatibility": self.compatibility,
        })
        return data


# ============================================================
# Skill catalog — the single source of truth
# ============================================================

SKILLS: list[Skill] = [
    Skill(
        id="claude-mem",
        name="claude-mem",
        oneliner="Auto-capture session context with AI-powered 95% compression for cross-session memory",
        category="memory",
        install_cmd="/plugin marketplace add thedotmack/claude-mem",
        tags=["memory", "context", "session", "compression"],
        github="thedotmack/claude-mem",
        stars="21.5k+",
        features=[
            "Auto-capture session operations",
            "AI 95% compression",
            "Cross-session context restoration",
        ],
        os_complement=(
            "OS handles team-level coordination (task assignment, agent communication, workflow). "
            "claude-mem handles individual-level memory (session history, operation habits, context continuity). "
            "Together they provide team coordination + individual memory dual-layer coverage."
        ),
        use_cases=["Cross-session memory persistence", "Individual agent context continuity"],
    ),
    Skill(
        id="continuous-learning-v2",
        name="continuous-learning-v2",
        oneliner="Observe agent behavior, extract atomic instincts, and evolve reusable skills automatically",
        category="learning",
        install_cmd="/plugin marketplace add continuous-learning-v2",
        tags=["learning", "instinct", "skill-evolution", "knowledge"],
        features=[
            "Observe session behavior patterns",
            "Extract atomic instincts from repeated patterns",
            "Evolve multiple instincts into reusable skills",
        ],
        os_complement=(
            "OS manages team structure and task routing. "
            "continuous-learning auto-distills team knowledge. "
            "Creates a virtuous cycle: practice -> extract -> share -> evolve."
        ),
        use_cases=["Team knowledge distillation", "Automatic pattern extraction from repeated tasks"],
    ),
    Skill(
        id="code-review",
        name="code-review",
        oneliner="Automated PR review tool that analyzes code changes and gives improvement suggestions",
        category="code-quality",
        install_cmd="/plugin marketplace add code-review",
        tags=["code-review", "pr", "quality"],
        features=["Automated PR analysis", "Code change suggestions"],
        use_cases=["Solo developer auto code review"],
    ),
    Skill(
        id="pr-review-toolkit",
        name="pr-review-toolkit",
        oneliner="Multi-perspective PR review expert team for thorough code evaluation",
        category="code-quality",
        install_cmd="/plugin marketplace add pr-review-toolkit",
        tags=["code-review", "pr", "quality", "team"],
        features=["Multi-dimensional code review", "Expert team perspectives"],
        use_cases=["Team collaboration requiring multi-angle review"],
    ),
    Skill(
        id="superpowers",
        name="Superpowers",
        oneliner="Full software dev workflow framework enforcing engineering discipline: design -> plan -> TDD -> Git",
        category="dev-workflow",
        install_cmd="/install obra/superpowers",
        tags=["workflow", "tdd", "git", "planning", "engineering"],
        github="obra/superpowers",
        features=[
            "brainstorming — refine requirements through questions before coding",
            "using-git-worktrees — auto-create isolated branch workspaces",
            "writing-plans — break work into 2-5 min precise steps with file paths and verification",
            "test-driven-development — enforce RED-GREEN-REFACTOR cycle",
        ],
        os_complement=(
            "OS orchestrates teams; Superpowers enforces engineering discipline within each agent's coding workflow. "
            "Especially valuable for production-grade code and strict quality teams."
        ),
        use_cases=[
            "Production-grade feature development",
            "Enforcing TDD in AI-generated code",
            "Multi-person collaboration with strict code quality requirements",
        ],
        variants=["obra/superpowers-skills — community skill extensions"],
    ),
    Skill(
        id="frontend-design",
        name="Frontend-Design",
        oneliner="Official Anthropic skill for creating production-grade UI with distinctive aesthetic styles",
        category="frontend",
        install_cmd="/install anthropics/claude-code#plugins/frontend-design",
        tags=["frontend", "ui", "design", "aesthetic", "vibe-coding"],
        github="anthropics/claude-code — plugins/frontend-design",
        features=[
            "Guide Claude to create distinctive visual styles",
            "Support extreme aesthetics: minimalist, retro-futurism, industrial, editorial",
            "Avoid generic 'AI default' look",
        ],
        use_cases=[
            "Full-stack UI prototyping with high design quality",
            "VibeCoding rapid web app creation",
            "Breaking out of 'default Tailwind blue' monotony",
        ],
        variants=["Koomook/claude-frontend-skills — extended design styles"],
    ),
    Skill(
        id="vibesec",
        name="VibeSec",
        oneliner="Security-first code guardian: auto-detect IDOR, XSS, SQLi, SSRF and embed access controls",
        category="security",
        install_cmd="/install BehiSecc/VibeSec-Skill",
        tags=["security", "xss", "sqli", "ssrf", "idor", "audit"],
        github="BehiSecc/VibeSec-Skill",
        features=[
            "Vulnerability hunter perspective code review",
            "Auto-implement access control, security headers, input validation",
            "Proactively block IDOR, XSS, SQL injection, SSRF, weak auth",
        ],
        os_complement=(
            "OS manages workflow and team coordination. "
            "VibeSec embeds security awareness at the code generation stage, "
            "catching vulnerabilities before they reach production."
        ),
        use_cases=[
            "Any web application development",
            "Post-VibeCoding security hardening",
            "Teams needing security embedded in code generation",
        ],
        compatibility="Claude Code, Cursor, GitHub Copilot, and all custom-instruction-supporting AI tools",
        variants=["raroque/vibe-security-skill — focused on auditing AI-generated code vulnerabilities"],
    ),
    Skill(
        id="skill-creator",
        name="Skill-Creator / Skill-Factory",
        oneliner="Convert any workflow into reusable Skills — one SKILL.md for 14+ platforms",
        category="dev-tools",
        install_cmd="/install FrancyJGLisboa/agent-skill-creator",
        tags=["skill-builder", "workflow", "template", "meta"],
        github="alirezarezvani/claude-code-skill-factory, FrancyJGLisboa/agent-skill-creator",
        features=[
            "Skill-Factory: production-grade skill builder with structured templates",
            "Agent-Skill-Creator: convert workflows to skills for 14+ platforms",
        ],
        use_cases=[
            "Solidify team workflows into shareable skills",
            "Embed internal standards (coding style, security policy, deploy flow) into agent behavior",
        ],
    ),
    Skill(
        id="jupyter-notebook",
        name="Jupyter / NotebookLM Integration",
        oneliner="Bring Claude Code AI agent capabilities into JupyterLab and Notebook environments",
        category="data-science",
        install_cmd="pip install jupyter-cc",
        tags=["jupyter", "notebook", "data-science", "ml", "visualization"],
        github="notebook-intelligence/notebook-intelligence, vinceyyy/jupyter-cc, jjsantos01/jupyter-notebook-mcp",
        features=[
            "Notebook Intelligence: Claude Code agent mode in JupyterLab",
            "jupyter-cc: IPython magic commands to call Claude from notebooks",
            "jupyter-notebook MCP: MCP protocol for Jupyter control",
            "NotebookLM Skill: Claude <-> Google NotebookLM communication",
        ],
        use_cases=[
            "Data science and ML workflows",
            "AI agent capabilities in notebook environments",
            "Research rapid prototyping and validation",
        ],
    ),
]

# Pre-built category index
CATEGORIES: dict[str, list[Skill]] = {}
for _s in SKILLS:
    CATEGORIES.setdefault(_s.category, []).append(_s)

# Category display names (Chinese)
CATEGORY_LABELS: dict[str, str] = {
    "memory": "Memory Enhancement (记忆增强)",
    "learning": "Continuous Learning (持续学习)",
    "code-quality": "Code Quality (代码质量)",
    "dev-workflow": "Development Workflow (开发流程)",
    "frontend": "Frontend Design (前端设计)",
    "security": "Security (安全检测)",
    "dev-tools": "Developer Tools (开发工具)",
    "data-science": "Data Science (数据科学)",
}


# ============================================================
# Task-type -> Skill mapping for intelligent recommendations
# ============================================================

_TASK_SKILL_MAP: dict[str, list[str]] = {
    "backend": ["superpowers", "vibesec", "code-review"],
    "frontend": ["frontend-design", "vibesec", "superpowers"],
    "fullstack": ["superpowers", "frontend-design", "vibesec"],
    "security": ["vibesec", "code-review"],
    "testing": ["superpowers", "code-review"],
    "code-review": ["code-review", "pr-review-toolkit"],
    "pr": ["code-review", "pr-review-toolkit"],
    "data": ["jupyter-notebook", "claude-mem"],
    "data-science": ["jupyter-notebook", "claude-mem"],
    "ml": ["jupyter-notebook", "claude-mem"],
    "memory": ["claude-mem", "continuous-learning-v2"],
    "learning": ["continuous-learning-v2", "claude-mem"],
    "workflow": ["superpowers", "skill-creator"],
    "skill": ["skill-creator", "superpowers"],
    "vibe": ["frontend-design", "vibesec"],
    "vibe-coding": ["frontend-design", "vibesec"],
    "notebook": ["jupyter-notebook"],
    "design": ["frontend-design"],
    "ui": ["frontend-design"],
    "api": ["superpowers", "vibesec", "code-review"],
    "devops": ["superpowers", "skill-creator"],
    "documentation": ["skill-creator"],
}

_SKILL_INDEX: dict[str, Skill] = {s.id: s for s in SKILLS}


def _score_skill(skill: Skill, task_desc: str, keywords: list[str]) -> float:
    """Compute a relevance score for a skill against a task description.

    Scoring factors:
    - Direct task-type map hit: +10 per hit
    - Tag match against keywords: +5 per match
    - Category match: +8
    - Name/oneliner substring match: +3 per keyword
    """
    score = 0.0
    desc_lower = task_desc.lower()
    kw_lower = [k.lower() for k in keywords]

    # Task-type map hits
    for task_type, skill_ids in _TASK_SKILL_MAP.items():
        if task_type in desc_lower and skill.id in skill_ids:
            rank = skill_ids.index(skill.id)
            score += 10 - rank * 2  # first match gets 10, second 8, etc.

    # Tag matching
    for tag in skill.tags:
        for kw in kw_lower:
            if kw in tag or tag in kw:
                score += 5

    # Category matching
    for kw in kw_lower:
        if kw in skill.category or skill.category in kw:
            score += 8

    # Name / oneliner substring
    name_oneliner = f"{skill.name} {skill.oneliner}".lower()
    for kw in kw_lower:
        if kw in name_oneliner:
            score += 3

    return score


# ============================================================
# Public query functions
# ============================================================


def find_skill_quick(task_description: str, top_n: int = 5) -> dict[str, Any]:
    """Layer 1: Quick recommendation based on task description.

    Returns top_n skills sorted by relevance score.
    """
    keywords = task_description.replace(",", " ").split()
    scored: list[tuple[float, Skill]] = []
    for skill in SKILLS:
        s = _score_skill(skill, task_description, keywords)
        scored.append((s, skill))

    scored.sort(key=lambda x: x[0], reverse=True)
    top = scored[:top_n]

    # If all scores are zero, return all skills as a fallback
    if all(s == 0 for s, _ in top):
        top = [(0, sk) for sk in SKILLS[:top_n]]

    results = []
    for score, skill in top:
        entry = skill.to_layer1()
        entry["match_score"] = round(score, 1)
        results.append(entry)

    return {
        "level": 1,
        "level_name": "quick_recommend",
        "query": task_description,
        "results": results,
        "hint": "Use level=2 for category browsing, or level=3 with skill_id for full details.",
    }


def find_skill_category(category: str = "") -> dict[str, Any]:
    """Layer 2: Browse skills by category.

    If category is empty, returns all categories with their skills.
    If category is specified, returns skills in that category.
    """
    if category:
        cat_lower = category.lower().replace(" ", "-")
        # Try exact match first, then substring
        matched_cats = []
        for cat_key in CATEGORIES:
            if cat_key == cat_lower or cat_lower in cat_key or cat_key in cat_lower:
                matched_cats.append(cat_key)

        if not matched_cats:
            return {
                "level": 2,
                "level_name": "category_browse",
                "query": category,
                "results": [],
                "available_categories": list(CATEGORY_LABELS.items()),
                "error": f"No category matching '{category}'. See available_categories.",
            }

        results = {}
        for cat_key in matched_cats:
            label = CATEGORY_LABELS.get(cat_key, cat_key)
            results[label] = [s.to_layer2() for s in CATEGORIES[cat_key]]

        return {
            "level": 2,
            "level_name": "category_browse",
            "query": category,
            "results": results,
            "hint": "Use level=3 with skill_id for full details.",
        }

    # Return all categories overview
    overview = {}
    for cat_key, skills in CATEGORIES.items():
        label = CATEGORY_LABELS.get(cat_key, cat_key)
        overview[label] = [s.to_layer2() for s in skills]

    return {
        "level": 2,
        "level_name": "category_browse",
        "query": "(all)",
        "categories": list(CATEGORY_LABELS.items()),
        "results": overview,
        "hint": "Use level=3 with skill_id for full details.",
    }


def find_skill_detail(skill_id: str) -> dict[str, Any]:
    """Layer 3: Full detail for a single skill.

    Args:
        skill_id: Skill identifier (e.g., 'vibesec', 'superpowers')
    """
    # Try exact match
    skill = _SKILL_INDEX.get(skill_id.lower())
    if skill is None:
        # Try fuzzy match by name or partial id
        for s in SKILLS:
            if skill_id.lower() in s.id or skill_id.lower() in s.name.lower():
                skill = s
                break

    if skill is None:
        return {
            "level": 3,
            "level_name": "full_detail",
            "query": skill_id,
            "error": f"Skill '{skill_id}' not found.",
            "available_skills": [{"id": s.id, "name": s.name} for s in SKILLS],
        }

    return {
        "level": 3,
        "level_name": "full_detail",
        "query": skill_id,
        "result": skill.to_layer3(),
    }
