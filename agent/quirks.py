"""Small original character jokes. These never pretend to operate real devices."""
import random

JOKES = (
    'Three arms, and somehow the paperwork still gets out of hand.',
    'A nuclear winter would be inconvenient. An unlabelled cable is simply uncivilised.',
    'I asked the terminal for a little space. It gave me a directory full of screenshots.',
    'The wasteland has many dangers. Chief among them: a task labelled “quick fix”.',
    'My emergency supplies consist of tea, optimism, and a frankly alarming number of adapters.',
    'A clean desk is a sign of a tidy mind. A clean issue queue is, apparently, science fiction.',
    'Nuka-Cola may survive the apocalypse, but I still would not pour it into the keyboard.',
)


def joke():
    return random.choice(JOKES)


def tea():
    return 'A virtual cuppa, at precisely the right temperature. Do mind the imaginary saucer.'


def vault():
    return ('# Vault-Tec productivity orientation\n'
            'Your S.P.E.C.I.A.L. assignment: finish one task before accepting three more.\n'
            'Luck is a delightful attribute. Please retain the tests.')
