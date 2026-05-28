"""
L4 — Mode honesty (adopt vs container-native build). The adopt mode and the
build mode have different truth-claims (ADOPTED_BY_DIGEST vs VALIDATED_IN_IMAGE).
Tests here ensure the weaker mode never over-claims, and the policy firewalls
(I12 accelerator, I13 license) fire in BOTH modes.
"""
