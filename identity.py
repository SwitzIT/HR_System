"""Candidate identity (PAN or passport): validation, keyed hashing and masking.

The raw ID number is never written to the database. We keep:
  * id_hash  - HMAC-SHA256(ID_HASH_KEY, "type:country:NUMBER"), to recognise the same person again
               (one account per ID, and the re-apply waiting period follows the person, not the email);
  * id_last4 - last 4 characters, so HR can confirm the ID with the candidate if needed.
"""
import hashlib
import hmac
import re

from flask import current_app

# PAN: 5 letters, 4 digits, 1 letter; the 4th letter is the holder type, "P" for an individual.
PAN_RE = re.compile(r"^[A-Z]{3}P[A-Z][0-9]{4}[A-Z]$")
PAN_SHAPE_RE = re.compile(r"^[A-Z]{5}[0-9]{4}[A-Z]$")
# Passport formats differ by country, so accept 6-12 letters/digits.
PASSPORT_RE = re.compile(r"^[A-Z0-9]{6,12}$")

ID_TYPES = {"pan": "PAN card", "passport": "Passport"}


class IdentityError(ValueError):
    pass


def normalise(id_type, number, country=""):
    """Validate and return (id_type, country, canonical_number). Raises IdentityError."""
    id_type = (id_type or "").strip().lower()
    num = re.sub(r"[\s\-]", "", (number or "")).upper()
    country = (country or "").strip()
    if id_type not in ID_TYPES:
        raise IdentityError("Choose PAN card or Passport.")
    if not num:
        raise IdentityError("Enter your ID number.")
    if id_type == "pan":
        if not PAN_SHAPE_RE.match(num):
            raise IdentityError("A PAN has 10 characters: 5 letters, 4 digits, 1 letter (e.g. ABCPE1234F).")
        if not PAN_RE.match(num):
            raise IdentityError("This PAN isn't an individual's PAN: the 4th letter should be P.")
        return "pan", "IN", num
    if not PASSPORT_RE.match(num):
        raise IdentityError("A passport number should be 6–12 letters and digits.")
    if not country:
        raise IdentityError("Enter the country that issued your passport.")
    return "passport", country.upper()[:60], num


def id_hash(id_type, country, canonical):
    key = current_app.config["ID_HASH_KEY"].encode()
    msg = f"{id_type}:{country}:{canonical}".encode()
    return hmac.new(key, msg, hashlib.sha256).hexdigest()


def apply_to_user(user, id_type, number, country=""):
    """Validate, check uniqueness and set the identity fields on `user`. Returns the hash."""
    from models import User, now
    t, c, canon = normalise(id_type, number, country)
    h = id_hash(t, c, canon)
    other = User.query.filter(User.id_hash == h, User.id != (user.id or 0)).first()
    if other:
        raise IdentityError("An account with this ID already exists. Please sign in with that account "
                            "(use the email you registered with), or contact HR.")
    user.id_type, user.id_country, user.id_hash, user.id_last4 = t, c, h, canon[-4:]
    user.id_consent_at = now()
    return h
