"""File discovery adapters — wrap OS-native indexers and CLI tools.

Per spec §17.4 the engine prefers fast OS-native indexes (Spotlight on macOS,
Everything on Windows, plocate on Linux). The pure-Python walker is always
available as a last-resort fallback. Adapter selection happens in
``office_layer.adapters.registry``.
"""
