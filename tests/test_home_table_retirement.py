from camellia.database import Database


def test_outdated_home_table_knowledge_is_archived_with_history(tmp_path):
    database = Database(str(tmp_path / "retirement.db"))
    knowledge_id = database.add_learned_knowledge(
        "Bengali Home Table",
        "Tell me more",
        (
            "Hi! Thank you for connecting. Sharing details of the Bengali Home Table, "
            "4th & 11th October 2026, Diamond Park, Joka, Kolkata. Bengali omakase "
            "for Male Folk Deities of Bengal during Durga Pujo."
        ),
        aliases=["More details", "Know more", "Want to know more"],
    )

    database.init_db()

    record = database.get_custom_qa(knowledge_id)
    assert record["status"] == "ARCHIVED"
    assert database.search_learned_knowledge("Tell me more") is None
    history = database.get_learned_knowledge_versions(knowledge_id)
    assert history and history[0]["change_reason"] == "retired_outdated_home_table_event"
    assert {"More details", "Know more", "Want to know more"}.issubset(
        set(history[0]["aliases_snapshot"])
    )
