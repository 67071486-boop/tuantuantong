import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase


def load_operations():
    path = Path(__file__).resolve().parents[2] / "deploy" / "wecom-archive" / "zone_operations.py"
    spec = importlib.util.spec_from_file_location("wecom_zone_operations", path)
    operations = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(operations)
    return operations


class CapturingSdk:
    def __init__(self, method, result):
        self.method = method
        self.result = result
        self.calls = []

    def __getattr__(self, method):
        if method != self.method:
            raise AssertionError(f"unexpected call: {method}")

        def invoke(body):
            self.calls.append(json.loads(body))
            return json.dumps(self.result, ensure_ascii=False)

        return invoke


class ZoneOperationTests(TestCase):
    def test_sdk_receives_escaped_json_that_preserves_chinese_rule_text(self):
        operations = load_operations()
        request = {"name": "商家退款", "keyword": {"word_list": ["原路退回"]}}

        def create_rule(body):
            self.assertTrue(body.isascii())
            self.assertEqual(json.loads(body), request)
            return '{"errcode": 0, "rule_id": "fixture-rule"}'

        sdk = SimpleNamespace(create_rule=create_rule)
        self.assertEqual(operations.run_operation(sdk, "create_rule", request),
                         {"errcode": 0, "rule_id": "fixture-rule"})

    def test_sync_keeps_ids_encrypted_key_and_metadata_only(self):
        operations = load_operations()
        sdk = CapturingSdk("sync_msg", {"errcode": 0, "msg_list": [{
            "msgid": "fixture-msg", "chatid": "fixture-room", "msgtype": 6, "send_time": 42,
            "service_encrypt_info": {"encrypted_secret_key": "fixture-cipher", "public_key_ver": 2,
                                     "secret_key": "discard-clear-key"},
            "sender": {"id": "fixture-user", "type": 1, "name": "discard-name"},
            "receiver_list": [{"id": "fixture-contact", "type": 2}],
            "extra_info": {"call_duration": 10, "content": "discard-clear-data"},
            "content": "discard-raw-message",
        }], "has_more": 1, "next_cursor": "fixture-next", "access_token": "discard-token"})
        request = {"cursor": "fixture-current", "mode": 2, "limit": 1000}
        self.assertEqual(operations.run_operation(sdk, "sync_msg", request), {
            "errcode": 0, "msg_list": [{
                "msgid": "fixture-msg", "chatid": "fixture-room", "msgtype": 6, "send_time": 42,
                "service_encrypt_info": {"encrypted_secret_key": "fixture-cipher", "public_key_ver": 2},
                "sender": {"id": "fixture-user", "type": 1},
                "receiver_list": [{"id": "fixture-contact", "type": 2}],
                "extra_info": {"call_duration": 10},
            }], "has_more": 1, "next_cursor": "fixture-next",
        })
        self.assertEqual(sdk.calls, [request])

    def test_every_documented_request_projects_its_own_successful_result(self):
        operations = load_operations()
        message = {"msg_list": [
            {"msgid": "fixture-msg", "encrypt_info": {"secret_key": "fixture-clear-key"}},
        ]}
        chat = {"chatid": "fixture-room"}
        query = {"query_word": "合成查询", "limit": 32, "cursor": ""}
        job = {"jobid": "fixture-job"}
        task = {"jobid": "fixture-job", "fail_list": []}
        document = {"doc_list": [{"doc_name": "fixture.pdf", "type": 2, "file_media_id": "fixture-media"}]}
        knowledge = {"kb_id": "fixture-kb"}
        rule = {"rule_id": "fixture-rule"}
        result = {"status": 1, "response_data": "合成摘要"}
        methods = {
            "sync_msg": ({"mode": 1}, {"page_id_list": ["fixture-page"], "has_more": 1}),
            "get_msg_list_by_page_id": ({"page_id": "fixture-page"}, {"msg_list": [{"msgid": "fixture-msg"}]}),
            "get_agree_status_single": ({"item": [{"userid": "fixture-user", "external_userid": "fixture-contact"}]},
                                        {"agreeinfo": [{"agree_status": "Disagree", "status_change_time": 42}]}),
            "get_agree_status_room": (chat, {"agreeinfo": [{"agree_status": "Agree", "external_userid": "fixture-contact"}]}),
            "get_group_chat": (chat, {"creator": "fixture-user", "members": [{"memberid": "fixture-contact", "type": 2}]}),
            "search_chat": (query, {"chat_list": [chat], "has_more": 0}),
            "search_msg": ({**query, "chat_info": {"chat_type": 2, "chat_id": "fixture-room", "msg_type_list": [8]},
                            "skip_stop_words": False, "start_time": 42, "end_time": 64},
                           {"msg_list": [{"msgid": "fixture-msg"}]}),
            "search_contact_or_customer": ({**query, "query_user_type": 2},
                                           {"user_list": [{"id": "fixture-contact", "type": 2}]}),
            "create_rule": ({"name": "合成规则", "keyword": {"word_list": ["合成"]}}, rule),
            "get_rule_list": ({"limit": 24}, {"rule_list": [{**rule, "create_time": 42}], "has_more": False}),
            "get_rule_detail": (rule, {"name": "合成规则", "applicable_range": {"target_type": {"type_list": [1]}}}),
            "update_rule": ({**rule, "applicable_range": {"chat": {"id_list": []}}}, {}),
            "delete_rule": (rule, {}),
            "get_hit_msg_list": ({"token": "fixture-token", "need_detail": 1}, {"msg_list": [{
                "msgid": "fixture-msg", "msg_detail": {"msgtype": 2},
                "hit_rule_list": [{**rule, "semantics_list": [5], "has_hit_keyword": False}],
            }]}),
            "knowledge_base_list": ({}, {"kb_info_list": [{**knowledge, "kb_name": "合成资料"}]}),
            "knowledge_base_create": ({**document, "kb_name": "合成资料"}, knowledge),
            "knowledge_base_detail": (knowledge, {**knowledge, **document}),
            "knowledge_base_add_doc": ({**knowledge, **document}, {
                "rejected_item_list": [{"doc_name": "fixture.pdf", "reject_errcode": 710733}],
            }),
            "knowledge_base_remove_doc": ({**knowledge, "docid_list": [0, 4096]}, {}),
            "knowledge_base_modify_name": ({**knowledge, "kb_name": "合成资料二"}, {}),
            "knowledge_base_delete": (knowledge, {}),
            "create_ww_model_task": ({**message, "model_id": "fixture-official-model", "need_think_result": False}, task),
            "get_ww_model_result": (job, {**result, "filtered_content_list": [{"docid": "fixture-doc", "errcode": 42}]}),
            "create_recommend_dialog_task": ({**message, **knowledge}, task),
            "get_recommend_dialog_result": (job, result),
            "create_customer_tag_task": ({**message, "tag_group_list": [{"group_id": "fixture-tag-group"}]}, task),
            "get_customer_tag_result": (job, {"status": 1, "tag_result_list": [
                {"group_id": "fixture-tag-group", "tag_id": "fixture-tag"},
            ]}),
            "create_summary_task": (message, task),
            "get_summary_result": (job, result),
            "create_sentiment_task": (message, task),
            "get_sentiment_result": (job, {"status": 1, "analyze_result_list": [
                {"errcode": 0, "msgid": "fixture-msg", "sentiment_result": 2},
            ]}),
            "create_model_task": ({**message, "model_id": "fixture-model", "ability_id": "fixture-ability"}, task),
            "get_model_task_result": (job, {"response_errcode": 0, "response_data": '{"output": "合成结果"}'}),
            "create_spam_task": (message, task),
            "get_spam_result": (job, {"status": 1, "analyze_result_list": [
                {"errcode": 0, "msgid": "fixture-msg", "spam_result": 0},
            ]}),
            "create_chatdata_export_job": ({"code": "fixture-code", "media_id": "fixture-media"}, job),
            "get_chatdata_export_job_status": (job, {"status": 3, "result_errcode": 0, "result_id": "fixture-result"}),
            "create_program_task": ({**message, "program_id": "fixture-other-program", "ability_id": "fixture-analyze"}, task),
            "get_program_task_result": (job, {"response_errcode": 0, "response_data": '{"output": "合成结果"}'}),
        }
        self.assertEqual(operations.OPERATION_NAMES, set(methods))
        for method, (request, response) in methods.items():
            with self.subTest(method=method):
                sdk = CapturingSdk(method, {"errcode": 0, "errmsg": "ok", "secret_key": "fixture-private", **response})
                self.assertEqual(operations.run_operation(sdk, method, request), {"errcode": 0, **response})
                self.assertEqual(sdk.calls, [request])
                failed = CapturingSdk(method, {"errcode": 710005, "errmsg": "fixture-secret", **response})
                self.assertEqual(operations.run_operation(failed, method, request), {"errcode": 710005})

    def test_out_of_contract_requests_stop_before_any_sdk_call(self):
        operations = load_operations()
        bad_requests = [
            ("common_send_http_post", {}),
            ("program_async_job_call_back", {}),
            ("__getattribute__", {}),
            ([], {}),
            ("sync_msg", {"limit": True}),
            ("sync_msg", {"mode": 3}),
            ("get_hit_msg_list", {"cursor": "界" * 22}),
            ("search_chat", {"query_word": "字"}),
            ("search_msg", {"query_word": "合成", "end_time": 42}),
            ("search_msg", {"query_word": "合成", "start_time": 42, "end_time": 42 + 32 * 86400}),
            ("search_msg", {"query_word": "合成", "chat_info": {"chat_type": 1, "chat_id": "fixture-room"}}),
            ("search_contact_or_customer", {"query_word": "界" * 32}),
            ("create_rule", {"name": "合成规则"}),
            ("create_rule", {"name": "合成规则", "semantics": {"semantics_list": [13]}}),
            ("update_rule", {"rule_id": "fixture-rule", "applicable_range": {"wrong": []}}),
            ("knowledge_base_create", {"kb_name": "合成知识", "doc_list": [
                {"doc_name": "fixture.pdf", "file_media_id": "fixture-media", "online_docid": "fixture-doc", "type": 2},
            ]}),
            ("knowledge_base_delete", {"kb_id": "fixture-kb", "access_token": "caller-token"}),
            ("create_ww_model_task", {"model_id": "fixture-model"}),
            ("create_summary_task", {"msg_list": [{"msgid": "fixture-msg", "encrypt_info": {}}]}),
            ("create_program_task", {"program_id": "fixture-program", "ability_id": "fixture-ability",
                                     "debug_info": {"chat": "raw-chat"}, "msg_list": [
                                         {"msgid": "fixture-msg", "encrypt_info": {"secret_key": "fixture-secret"}},
                                     ]}),
            ("get_program_task_result", {"jobid": "fixture-job", "unexpected": True}),
        ]
        sdk = CapturingSdk("no-call-expected", {})
        for method, request in bad_requests:
            with self.subTest(method=method, request=request), self.assertRaises(ValueError):
                operations.run_operation(sdk, method, request)
        self.assertEqual(sdk.calls, [])

    def test_model_errors_and_structured_results_drop_echoed_sensitive_fields(self):
        operations = load_operations()
        started = CapturingSdk("create_summary_task", {
            "errcode": 0, "jobid": "fixture-job",
            "fail_list": [{"msgid": "fixture-msg", "errcode": 710601, "encrypt_info": {"secret_key": "clear-secret"}}],
        })
        self.assertEqual(operations.run_operation(started, "create_summary_task", {"msg_list": [
            {"msgid": "fixture-msg", "encrypt_info": {"secret_key": "clear-secret"}},
        ]}), {"errcode": 0, "jobid": "fixture-job", "fail_list": [{"msgid": "fixture-msg", "errcode": 710601}]})

        polled = CapturingSdk("get_program_task_result", {"errcode": 0, "response_errcode": 0, "response_data": json.dumps({
            "output": {"summary": "合成摘要", "rows": [{"chat_content": "raw-chat", "user_doc_ticket": "raw-ticket"}]},
            "access_token": "raw-token",
        })})
        self.assertEqual(operations.run_operation(polled, "get_program_task_result", {"jobid": "fixture-job"}), {
            "errcode": 0, "response_errcode": 0,
            "response_data": '{"output": {"summary": "合成摘要", "rows": [{}]}}',
        })

    def test_document_retrieval_accepts_authorized_ticket_without_returning_it(self):
        operations = load_operations()
        request = {"doc_retrieval_params": {
            "oper_userid": "fixture-user", "user_doc_ticket": "fixture-ticket", "keywords": ["文件"],
            "doc_type": 8, "docid_list": ["fixture-doc"], "creator_userid_list": ["fixture-creator"],
            "retrieved_knowledge_list": [{"fragment": "合成片段", "ref_id": "fixture-ref"}],
        }}
        sdk = CapturingSdk("create_ww_model_task", {"errcode": 0, "jobid": "fixture-job", **request})
        self.assertEqual(operations.run_operation(sdk, "create_ww_model_task", request), {"errcode": 0, "jobid": "fixture-job"})
        self.assertEqual(sdk.calls, [request])

    def test_malformed_success_response_cannot_look_like_no_results(self):
        operations = load_operations()
        for response in ({"errcode": True}, {"errcode": 0, "msg_list": {}}, {"msg_list": []}):
            sdk = CapturingSdk("sync_msg", response)
            with self.subTest(response=response), self.assertRaises(ValueError):
                operations.run_operation(sdk, "sync_msg", {})

    def test_custom_analysis_only_returns_structured_approved_outputs(self):
        operations = load_operations()
        job = {"jobid": "fixture-job"}
        invalid = CapturingSdk("get_program_task_result", {
            "errcode": 0, "response_errcode": 0, "response_data": '{"content": "unclosed conversation"',
        })
        with self.assertRaises(ValueError):
            operations.run_operation(invalid, "get_program_task_result", job)
        aliases = CapturingSdk("get_model_task_result", {
            "errcode": 0, "response_errcode": 0, "response_data": json.dumps({
                "output": {"summary": "合成摘要", "encryptedSecretKey": "private-cipher", "chat-content": "raw-chat"},
            }),
        })
        result = operations.run_operation(aliases, "get_model_task_result", job)
        self.assertEqual(json.loads(result["response_data"]), {"output": {"summary": "合成摘要"}})

    def test_custom_debug_inputs_require_explicit_sdk_debug_mode_and_known_fields(self):
        operations = load_operations()
        message = [{"msgid": "fixture-msg", "encrypt_info": {"secret_key": "fixture-clear-key"}}]
        debug_info = {"chat": "员工说：合成消息", "chat_content": "合成消息"}
        sdk = CapturingSdk("create_program_task", {"errcode": 0, "jobid": "fixture-job"})
        request = {"program_id": "fixture-program", "ability_id": "fixture-ability",
                   "msg_list": message, "debug_info": debug_info}
        with self.assertRaises(ValueError):
            operations.run_operation(sdk, "create_program_task", request)
        with self.assertRaises(ValueError):
            operations.run_operation(sdk, "create_program_task", {**request, "debug_info": {"chat": "合成"}}, debug=True)
        self.assertEqual(sdk.calls, [])
        self.assertEqual(operations.run_operation(sdk, "create_program_task", request, debug=True), {
            "errcode": 0, "jobid": "fixture-job",
        })
        self.assertEqual(sdk.calls, [request])

        model_request = {"model_id": "fixture-model", "ability_id": "fixture-ability", "msg_list": message,
                         "debug_info": {**debug_info, "knowledge": "合成资料", "tagjson": "[]"}}
        sdk = CapturingSdk("create_model_task", {"errcode": 0, "jobid": "fixture-job"})
        self.assertEqual(operations.run_operation(sdk, "create_model_task", model_request, debug=True), {
            "errcode": 0, "jobid": "fixture-job",
        })
        self.assertEqual(sdk.calls, [model_request])

    def test_single_agreement_batches_are_limited_to_one_hundred(self):
        operations = load_operations()
        sdk = CapturingSdk("get_agree_status_single", {"errcode": 0, "agreeinfo": []})
        items = [{"userid": "fixture-staff", "external_userid": f"fixture-contact-{index}"} for index in range(101)]
        with self.assertRaises(ValueError):
            operations.run_operation(sdk, "get_agree_status_single", {"item": items})
        self.assertEqual(sdk.calls, [])
        self.assertEqual(operations.run_operation(sdk, "get_agree_status_single", {"item": items[:100]}),
                         {"errcode": 0, "agreeinfo": []})

    def test_keyword_match_options_require_words_on_create_and_update(self):
        operations = load_operations()
        for method, request in (
            ("create_rule", {"name": "测试规则", "semantics": {"semantics_list": [1]}}),
            ("update_rule", {"rule_id": "fixture-rule"}),
        ):
            with self.subTest(method=method):
                sdk = CapturingSdk(method, {"errcode": 0})
                for keyword in ({"is_case_sensitive": 0}, {"word_list": [], "match_rule": 1}):
                    with self.assertRaises(ValueError):
                        operations.run_operation(sdk, method, {**request, "keyword": keyword})
                self.assertEqual(sdk.calls, [])
                body = {**request, "keyword": {"word_list": ["合成"], "is_case_sensitive": 0, "match_rule": 1}}
                self.assertEqual(operations.run_operation(sdk, method, body), {"errcode": 0})
                self.assertEqual(sdk.calls, [body])

    def test_official_analysis_keeps_prose_starting_with_punctuation(self):
        operations = load_operations()
        for method, output in (
            ("get_summary_result", "[退款相关] 正在跟进，客户需要确认。"),
            ("get_ww_model_result", "{跟进建议} 建议先确认订单信息。"),
            ("get_recommend_dialog_result", ["[推荐回复] 会尽快处理。"]),
        ):
            with self.subTest(method=method):
                sdk = CapturingSdk(method, {"errcode": 0, "status": 1, "response_data": output})
                self.assertEqual(operations.run_operation(sdk, method, {"jobid": "fixture-job"}),
                                 {"errcode": 0, "status": 1, "response_data": output})

    def test_configured_analysis_text_fields_survive_except_raw_source_or_credentials(self):
        operations = load_operations()
        upstream = {"output": {"text": "合成加工结果", "content": "合成分析", "chat": {"text": "原始会话"},
                               "messages": [{"content": "原始消息"}], "accessToken": "fixture-private"}}
        sdk = CapturingSdk("get_program_task_result", {
            "errcode": 0, "response_errcode": 0, "response_data": json.dumps(upstream, ensure_ascii=False),
        })
        result = operations.run_operation(sdk, "get_program_task_result", {"jobid": "fixture-job"})
        self.assertEqual(json.loads(result["response_data"]), {"output": {"text": "合成加工结果", "content": "合成分析"}})

    def test_failed_export_keeps_result_error_without_returning_a_download_id(self):
        operations = load_operations()
        sdk = CapturingSdk("get_chatdata_export_job_status", {
            "errcode": 0, "status": 3, "result_errcode": 710005, "result_id": "unexpected-download",
        })
        self.assertEqual(operations.run_operation(sdk, "get_chatdata_export_job_status", {"jobid": "fixture-job"}), {
            "errcode": 0, "status": 3, "result_errcode": 710005,
        })

    def test_analysis_rejects_sensitive_fields_pasted_as_broken_or_fenced_json(self):
        operations = load_operations()
        for output in (
            '{"secret_key": "fixture-message-key"',
            '~~~json\n{"output": "synthesis", "userDocTicket": "fixture-ticket"}\n~~~',
        ):
            with self.subTest(output=output), self.assertRaises(ValueError):
                sdk = CapturingSdk("get_summary_result", {"errcode": 0, "status": 1, "response_data": output})
                operations.run_operation(sdk, "get_summary_result", {"jobid": "fixture-job"})

    def test_analysis_redacts_credential_aliases_inside_nested_json_strings(self):
        operations = load_operations()
        nested = {
            "text": "合成说明", "content": "合成分析", "key": "公开业务编号", "status": "合成状态",
            "APIKey": "fixture-api-key", "X-API-Key": "fixture-header-key", "refresh-token": "fixture-refresh",
            "clientSecret": "fixture-app-secret", "password": "fixture-password", "private key": "fixture-private-key",
            "sessionToken": "fixture-session", "Authorization": "fixture-authorization",
        }
        sdk = CapturingSdk("get_ww_model_result", {
            "errcode": 0, "status": 1, "response_data": {"result": [json.dumps(nested, ensure_ascii=False)]},
        })
        result = operations.run_operation(sdk, "get_ww_model_result", {"jobid": "fixture-job"})
        self.assertEqual(json.loads(result["response_data"]["result"][0]), {
            "text": "合成说明", "content": "合成分析", "key": "公开业务编号", "status": "合成状态",
        })
