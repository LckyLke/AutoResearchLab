"""The model under optimization. AutoResearchLab agents may edit this file.

Contract (do not change): predict(xs) takes a list of floats and returns a
list of predicted y values of the same length.
"""


def predict(xs):
    # Deliberately weak starting point: predict a constant.
    return [0.0 for _ in xs]
