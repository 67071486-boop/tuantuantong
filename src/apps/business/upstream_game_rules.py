def allowed_upstream_game(binding, game_id):
    rules = binding.game_rules
    return rules is None or any(rule["game_id"] == game_id for rule in rules)


def upstream_game_price_percent(binding, game_id):
    if binding.game_rules is not None:
        for rule in binding.game_rules:
            if rule["game_id"] == game_id and rule["hall_price_percent"] is not None:
                return rule["hall_price_percent"]
    return binding.hall_price_percent or 100


def upstream_price_profile_changed(before, after):
    def rates_by_game(rules):
        if rules is None:
            return None
        return {rule["game_id"]: rule["hall_price_percent"] for rule in rules}

    return rates_by_game(before) != rates_by_game(after)
