"""Scientific evaluation against simulator ground truth (Phase 5+).

The only package allowed to read latent simulator parameters next to model output. Model
packages may never import it (import contract "Models never import the simulator").
"""
