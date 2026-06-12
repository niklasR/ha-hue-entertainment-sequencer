DOMAIN = "hue_entertainment"

CONF_BRIDGE_IP = "bridge_ip"
CONF_USERNAME = "username"
CONF_CLIENTKEY = "clientkey"
CONF_BRIDGE_ID = "bridge_id"

# Effect names (display strings — matched exactly in light.turn_on)
EFFECT_STROBE      = "Strobe"
EFFECT_FLASH       = "Flash"
EFFECT_PULSE       = "Pulse"
EFFECT_COLOR_CYCLE = "Color Cycle"
EFFECT_THEATER     = "Theater"   # 2-axis: color rotation + brightness pulse
EFFECT_CANDLE      = "Candle"
EFFECT_POLICE      = "Police"
EFFECT_CONFETTI    = "Confetti"
EFFECT_STATIC      = "Static"   # internal heartbeat, not shown in UI
EFFECT_SEQUENCE    = "Sequence" # keyframe sequence player

ALL_EFFECTS = [
    EFFECT_STROBE,
    EFFECT_FLASH,
    EFFECT_PULSE,
    EFFECT_COLOR_CYCLE,
    EFFECT_THEATER,
    EFFECT_CANDLE,
    EFFECT_POLICE,
    EFFECT_CONFETTI,
    EFFECT_SEQUENCE,
]

# Service names
SERVICE_START_EFFECT  = "start_effect"
SERVICE_STOP          = "stop"
SERVICE_CREATE_AREA   = "create_area"
SERVICE_UPDATE_AREA   = "update_area"
SERVICE_DELETE_AREA   = "delete_area"

# Live-parameter names (shared between stream + number entities)
PARAM_STROBE_HZ    = "strobe_hz"
PARAM_COLOR_SPEED  = "color_speed"
PARAM_PULSE_RATE   = "pulse_rate"
PARAM_BRIGHTNESS   = "brightness"
PARAM_COLOR        = "color"
PARAM_FLASH_COUNT  = "flash_count"
PARAM_SEQUENCE     = "sequence"

SERVICE_PLAY_SEQUENCE   = "play_sequence"
SERVICE_UPDATE_SEQUENCE = "update_sequence"
SERVICE_SAVE_SEQUENCE   = "save_sequence"
SERVICE_DELETE_SEQUENCE = "delete_sequence"

# Defaults
DEFAULT_STROBE_HZ     = 25.0
DEFAULT_FLASH_COUNT   = 3
DEFAULT_PULSE_RATE_HZ = 0.5
DEFAULT_CYCLE_SPEED   = 0.1
DEFAULT_BRIGHTNESS    = 1.0
