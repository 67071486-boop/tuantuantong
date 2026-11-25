MIB = 1024 * 1024
MAX_CHAT_IMAGE_BYTES = 3 * MIB
MAX_CHAT_ALBUM_BYTES = 21 * MIB // 4
MAX_CHAT_IMAGE_PIXELS = 16_000_000

# Base64 expands the total by 4/3. Leave room for per-image padding and JSON fields.
MAX_CHAT_MESSAGE_BODY = (MAX_CHAT_ALBUM_BYTES * 4 // 3) + 64 * 1024

# The legacy peer envelope can also carry a full order-group roster.
MAX_CHAT_PEER_BODY = (MAX_CHAT_ALBUM_BYTES * 4 // 3) + 5 * MIB // 2
