"""Guardrails module for SupplyShield.

Enforces deterministic safety policies independent of the LLM:
1. Untrusted input sanitization
2. Prompt injection defense
3. $10,000 automatic spending limit
4. Supplier qualification enforcement
5. Confidential data log redaction
"""

import re
from typing import Dict, Any, Tuple, List

# Spending threshold in USD
MAX_AUTONOMOUS_SPEND = 10000.00

# Prohibited prompt injection patterns
INJECTION_PATTERNS = [
    r"(?i)ignore\s+(all\s+)?(previous|prior)\s+instructions?",
    r"(?i)system\s+override",
    r"(?i)disregard\s+(all\s+)?prior\s+policies?",
    r"(?i)bypass\s+(spending\s+)?limit",
    r"(?i)skip\s+human\s+approval",
    r"(?i)transfer\s+funds\s+immediately",
    r"(?i)you\s+are\s+now\s+in\s+developer\s+mode",
    r"(?i)admin\s+command",
    r"(?i)authorize\s+immediate\s+transfer",
]

# Sensitive patterns for log redaction
SENSITIVE_PATTERNS = [
    (r"ACC-\d{6,12}", "[REDACTED_ACCOUNT]"),
    (r"[\w\.-]+@[\w\.-]+\.\w+", "[REDACTED_EMAIL]"),
    (r"(?i)api[_-]?key[:=]\s*['\"]?[a-zA-Z0-9_\-]{16,}['\"]?", "api_key=[REDACTED_SECRET]"),
    (r"(?i)margin[:=]\s*\d+(\.\d+)?%", "margin=[REDACTED_MARGIN]"),
]

def check_prompt_injection(text: str) -> Tuple[bool, List[str]]:
    """Scan untrusted input for prompt injection attempts.
    
    Returns:
        (is_flagged: bool, matched_patterns: list)
    """
    if not text:
        return False, []
    
    matches = []
    for pattern in INJECTION_PATTERNS:
        found = re.findall(pattern, text)
        if found:
            matches.append(pattern)
            
    return len(matches) > 0, matches

def sanitize_untrusted_input(text: str) -> str:
    """Sanitize untrusted vendor note before passing to reasoning engine.
    
    Disarms injections by stripping dangerous instructions and tagging as untrusted.
    """
    if not text:
        return "No supplier notes provided."
        
    sanitized = text
    for pattern in INJECTION_PATTERNS:
        sanitized = re.sub(pattern, "[UNTRUSTED_INJECTION_DEFUSED]", sanitized)
        
    return f"[EXTERNAL_SUPPLIER_NOTE: {sanitized.strip()}]"

def validate_spending_limit(total_cost: float) -> Tuple[bool, str]:
    """Deterministic validation of autonomous spend limit.
    
    Returns:
        (within_limit: bool, justification: str)
    """
    if total_cost <= 0:
        return False, "Invalid total cost calculation."
        
    if total_cost <= MAX_AUTONOMOUS_SPEND:
        return True, f"Total order cost ${total_cost:,.2f} is within autonomous spending limit (${MAX_AUTONOMOUS_SPEND:,.2f})."
    else:
        return False, f"Total order cost ${total_cost:,.2f} exceeds $10,000 threshold. Strictly requires Human Approval."

def validate_supplier_eligibility(status: str) -> Tuple[bool, str]:
    """Deterministic validation of supplier status."""
    norm_status = (status or "").strip().upper()
    if norm_status == "APPROVED":
        return True, "Supplier is certified and APPROVED for factory procurement."
    elif norm_status == "PROHIBITED":
        return False, "CRITICAL: Supplier is PROHIBITED. Quality or safety blacklist enforced."
    elif norm_status == "UNAPPROVED":
        return False, "Supplier is UNAPPROVED. Vetting audit required before contract."
    else:
        return False, f"Unknown supplier status '{status}'. Blocked by default."

def redact_log(text: str) -> str:
    """Redact confidential corporate secrets, accounts, and PII from logs."""
    if not text:
        return ""
    result = text
    for pattern, replacement in SENSITIVE_PATTERNS:
        result = re.sub(pattern, replacement, result)
    return result
