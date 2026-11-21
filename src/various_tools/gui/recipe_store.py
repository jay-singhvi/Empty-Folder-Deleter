"""The saved recipes as the GUI uses them: a list kept in memory, saved after every change.

The file format, atomic write and the "corrupt file is renamed aside" rule are in
``core/recipe.py``; this class only adds the editing operations the Recipes page and the tool
pages' "Save as recipe step" need. Names are unique without regard to letter case.
"""

from __future__ import annotations

import copy
from pathlib import Path

from various_tools.core.recipe import (
    Recipe,
    RecipeStep,
    load_recipes,
    recipes_path,
    save_recipes,
)


class RecipeStore:
    def __init__(self, settings_path: Path) -> None:
        self.path = recipes_path(settings_path)
        self.recipes: list[Recipe] = []
        self.notice: str | None = None
        self.reload()

    def reload(self) -> None:
        loaded = load_recipes(self.path)
        self.recipes = loaded.recipes
        self.notice = loaded.notice

    def save(self) -> None:
        save_recipes(self.path, self.recipes)

    def names(self) -> list[str]:
        return [r.name for r in self.recipes]

    def get(self, name: str) -> Recipe | None:
        wanted = name.strip().casefold()
        return next((r for r in self.recipes if r.name.casefold() == wanted), None)

    def create(self, name: str, steps: list[RecipeStep] | None = None) -> Recipe | None:
        """A new recipe, saved. None when the name is empty or already used."""
        name = name.strip()
        if not name or self.get(name) is not None:
            return None
        recipe = Recipe(name, list(steps or []))
        self.recipes.append(recipe)
        self.save()
        return recipe

    def add_step(self, name: str, step: RecipeStep) -> int:
        """Add a step to the recipe of that name (created when new). Returns its number."""
        recipe = self.get(name) or self.create(name)
        assert recipe is not None, "a recipe name must not be empty"
        recipe.steps.append(step)
        self.save()
        return len(recipe.steps)

    def rename(self, recipe: Recipe, new_name: str) -> bool:
        new_name = new_name.strip()
        other = self.get(new_name)
        if not new_name or (other is not None and other is not recipe):
            return False
        recipe.name = new_name
        self.save()
        return True

    def duplicate(self, recipe: Recipe) -> Recipe:
        base = f"{recipe.name} (copy)"
        name, number = base, 2
        while self.get(name) is not None:
            name, number = f"{base} {number}", number + 1
        clone = Recipe(name, copy.deepcopy(recipe.steps))
        self.recipes.insert(self.recipes.index(recipe) + 1, clone)
        self.save()
        return clone

    def delete(self, recipe: Recipe) -> None:
        self.recipes.remove(recipe)
        self.save()
