"""Readers for Capcom RE Engine file formats: ``.mesh`` models, ``.tex`` textures, ``.mdf2`` materials
and ``.pak`` archives.

Every RE Engine file carries its format version as a numeric extension (``pl0000.mesh.1808312334``);
the readers use that number to pick the layout of each game's generation.
"""
