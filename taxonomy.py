#!/usr/bin/env python3
"""The fixed classification vocabulary.

Everything the model is allowed to answer with lives here.  classify.py
inlines these lists into the prompts and validates replies against them, so a
label that isn't in this file can never reach the database.
"""

PROOF_METHODS = [
    "Argument by Contradiction",
    "Mathematical Induction",
    "The Pigeonhole Principle",
    "Ordered Sets and Extremal Elements",
    "Invariants and Semi-Invariants",
]

# topic -> domain -> [sub-domains]
TAXONOMY = {
    "Algebra": {
        "Identities and Inequalities": [
            "Algebraic Identities",
            "x^2 >= 0",
            "The Cauchy-Schwarz Inequality",
            "The Triangle Inequality",
            "The Arithmetic Mean-Geometric Mean Inequality",
            "Sturm's Principle",
            "Other Inequalities",
        ],
        "Polynomials": [
            "Viete's Relations",
            "The Derivative of a Polynomial",
            "The Location of the Zeros of a Polynomial",
            "Irreducible Polynomials",
            "Chebyshev Polynomials",
        ],
        "Linear Algebra": [
            "Operations with Matrices",
            "Determinants",
            "The Inverse of a Matrix",
            "Systems of Linear Equations",
            "Vector Spaces, Linear Combinations of Vectors, Bases",
            "Linear Transformations, Eigenvalues, Eigenvectors",
            "The Cayley-Hamilton and Perron-Frobenius Theorems",
        ],
        "Abstract Algebra": [
            "Binary Operations",
            "Groups",
            "Rings",
        ],
    },
    "Real Analysis": {
        "Sequences and Series": [
            "Linear Recursive Sequences",
            "Limits of Sequences",
            "Series",
            "Telescopic Series and Products",
        ],
        "Continuity, Derivatives, and Integrals": [
            "Limits of Functions",
            "Continuous Functions",
            "The Intermediate Value Property",
            "Derivatives and Their Applications",
            "The Mean Value Theorem",
            "Convex Functions",
            "Indefinite Integrals",
            "Definite Integrals",
            "Riemann Sums",
            "Inequalities for Integrals",
            "Taylor and Fourier Series",
        ],
        "Multivariable Differential and Integral Calculus": [
            "Partial Derivatives and Their Applications",
            "Multivariable Integrals",
            "The Many Versions of Stokes' Theorem",
        ],
        "Equations with Functions as Unknowns": [
            "Functional Equations",
            "Ordinary Differential Equations of the First Order",
            "Ordinary Differential Equations of Higher Order",
            "Problems Solved with Techniques of Differential Equations",
        ],
    },
    "Geometry and Trigonometry": {
        "Geometry": [
            "Vectors",
            "The Coordinate Geometry of Lines and Circles",
            "Conics and Other Curves in the Plane",
            "Coordinate Geometry in Three and More Dimensions",
            "Integrals in Geometry",
            "Other Geometry Problems",
        ],
        "Trigonometry": [
            "Trigonometric Identities",
            "Euler's Formula",
            "Trigonometric Substitutions",
            "Telescopic Sums and Products in Trigonometry",
        ],
    },
    "Number Theory": {
        "Integer-Valued Sequences and Functions": [
            "Fermat's Infinite Descent Principle",
            "The Greatest Integer Function",
        ],
        "Arithmetic": [
            "Factorization and Divisibility",
            "Prime Numbers",
            "Modular Arithmetic",
            "Fermat's Little Theorem",
            "Wilson's Theorem",
            "Euler's Totient Function",
            "The Chinese Remainder Theorem",
        ],
        "Diophantine Equations": [
            "Linear Diophantine Equations",
            "The Equation of Pythagoras",
            "Pell's Equation",
            "Other Diophantine Equations",
        ],
    },
    "Combinatorics and Probability": {
        "Combinatorial Arguments in Set Theory and Geometry": [
            "Set Theory and Combinatorics of Sets",
            "Permutations",
            "Combinatorial Geometry",
            "Euler's Formula for Planar Graphs",
            "Ramsey Theory",
        ],
        "Binomial Coefficients and Counting Methods": [
            "Combinatorial Identities",
            "Generating Functions",
            "Counting Strategies",
            "The Inclusion-Exclusion Principle",
        ],
        "Probability": [
            "Equally Likely Cases",
            "Establishing Relations Among Probabilities",
            "Geometric Probabilities",
        ],
    },
}

TOPICS = list(TAXONOMY)
DOMAINS = {d: t for t, ds in TAXONOMY.items() for d in ds}
SUBDOMAINS = {s: (t, d) for t, ds in TAXONOMY.items() for d, ss in ds.items() for s in ss}

MAX_SUBDOMAINS = 3


def topic_outline():
    """The whole taxonomy as an indented block for the prompts."""
    lines = []
    for topic, domains in TAXONOMY.items():
        lines.append(topic)
        for domain, subs in domains.items():
            lines.append(f"  {domain}")
            lines.extend(f"    - {s}" for s in subs)
    return "\n".join(lines)


def domains_of(topic):
    return list(TAXONOMY.get(topic, {}))


def subdomains_of(topic, domain):
    return list(TAXONOMY.get(topic, {}).get(domain, []))
