"""Selected gameplay state for text context; raw reflection remains queryable."""
import re

KEY_FIELD = re.compile(r'(?i)(speed|vel|state|timer|target|percent|node|dir|angle|radius|delay|wait|anger|chase|'
                       r'moving|active|triggered|falling|lerp|ease|progress|amount)')
_FLAGS = {'collected', 'isghostberry', 'flyingaway', 'winged', 'golden', 'moon', 'waitingonseeds',
          'activated', 'fallen', 'candash', 'twodashes', 'red', 'orient'}
_COSMETIC = re.compile(r'(?i)(image|sprite|graphic|bloom|wiggl|color|texture|sound|audio|particle|render)')
COSMETIC_TRIGGERS = ('Camera', 'Music', 'Ambience', 'Light', 'Bloom', 'Rumble', 'Sound', 'Altmusic',
                     'Minitext', 'Lookout', 'Spawn')


def selected_state(entity):
    """Stable, unrounded projection, not a claim of complete mechanism knowledge."""
    fields = entity.get('fields') or {}
    return {key: value for key, value in sorted(fields.items())
            if not _COSMETIC.search(key) and (KEY_FIELD.search(key) or key.lower() in _FLAGS)}
