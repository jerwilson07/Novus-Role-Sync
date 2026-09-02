import os


WOM_API_KEY = os.getenv("WOM_API_KEY")


def sanitize_rsn(rsn: str) -> str:
    return " ".join(
        rsn.replace("-", " ").replace("_", " ").split()
    )
