"""Durable game handoff for a Codex thread with an independently saved slot."""
import json


def stable_menu(obs):
    menu = obs.get('menu') or {}
    return (obs.get('scene') == 'Overworld' and obs.get('saving') is False and not menu.get('next')
            and menu.get('current') in {'OuiMainMenu', 'OuiTitleScreen', 'OuiFileSelect',
                                        'OuiChapterSelect', 'OuiChapterPanel'})


def parked_summary(directory):
    try:
        summary = json.loads((directory / 'summary.json').read_text(encoding='utf-8'))
    except (FileNotFoundError, ValueError):
        return {}
    return summary if summary.get('parked') is True else {}


def check_reserved_slot(root, cfg, previous):
    if cfg.create_save:
        return
    source = previous['dir'].resolve() if previous else None
    for file in root.glob('*/*/summary.json'):
        directory = file.parent
        if source == directory.resolve():
            continue
        summary = parked_summary(directory)
        if not summary or summary.get('resumed_by'):
            continue
        if (cfg.save_slot == summary['parked_slot'] if cfg.save_slot is not None
                else cfg.save == summary['parked_save']):
            raise ValueError(f'存档由挂起的试玩保留，请续跑该试玩或改用其他存档槽：{directory}')


def mark_resumed(directory, run_dir):
    summary = parked_summary(directory)
    if summary and not summary.get('resumed_by'):
        from .session_store import write_json
        summary['resumed_by'] = str(run_dir)
        write_json(directory / 'summary.json', summary)


def note_release(directory, *, released):
    """The normal bridge cleanup can retry a missing release acknowledgement."""
    from .session_store import write_json
    path = directory / 'summary.json'
    try:
        summary = json.loads(path.read_text(encoding='utf-8'))
    except (FileNotFoundError, ValueError):
        return False
    if summary.get('parked') is not True:
        return False
    summary['game_released'] = released
    write_json(path, summary)
    return True
