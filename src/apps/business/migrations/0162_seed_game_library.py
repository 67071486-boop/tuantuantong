"""冻结的 597 条快照：对标 597 行去重后 596 个唯一名，再加“蓝色星原：旅谣”。"""

import json
from pathlib import Path

from django.db import migrations


def seed_game_library(apps, schema_editor):
    GameLibrary = apps.get_model("business", "GameLibrary")
    data_file = Path(__file__).resolve().parent.parent / "data" / "game_library_0162.json"
    rows = json.loads(data_file.read_text(encoding="utf-8"))
    total = len(rows)
    GameLibrary.objects.bulk_create(
        [
            GameLibrary(name=name, first_key=first_key, sort_order=total - index)
            for index, (first_key, name) in enumerate(rows)
        ],
        ignore_conflicts=True,
    )


class Migration(migrations.Migration):
    dependencies = [("business", "0161_game_library")]

    operations = [migrations.RunPython(seed_game_library, migrations.RunPython.noop)]
