from log_analyzer.filters import filter_log


def test_filter_extracts_error_warn_lines():
    text = "\n".join(
        [
            "2026-05-06T14:22:01.012Z fw01 kernel: [INFO] interface eth0 link up",
            "2026-05-06T14:22:42.118Z fw01 fwd: [ERROR] DENY src=10.0.40.17",
            "2026-05-06T14:22:42.501Z fw01 fwd: [WARN] tcp retransmit",
            "2026-05-06T14:22:18.402Z fw01 fwd: [INFO] keepalive ok peer=10.0.0.1",
        ]
    )
    fr = filter_log(text)
    assert len(fr.anomaly_lines) == 2
    assert fr.normal_counts.get("keepalive") == 1
    assert fr.normal_counts.get("interface_up") == 1
    assert fr.other_info_count == 0


def test_compression_ratio_below_one_when_normal_dominates():
    text = "\n".join(
        [
            "[INFO] keepalive ok",
            "[INFO] keepalive ok",
            "[INFO] keepalive ok",
            "[INFO] keepalive ok",
            "[ERROR] DENY",
        ]
    )
    fr = filter_log(text)
    assert fr.compression_ratio < 1.0
    assert fr.original_bytes > fr.filtered_bytes


def test_unmatched_info_lines_counted_for_loss_flag():
    text = "\n".join(
        [
            "[INFO] something obscure happened",
            "[INFO] another oddity",
            "[ERROR] real problem",
        ]
    )
    fr = filter_log(text)
    assert fr.other_info_count == 2
    assert len(fr.anomaly_lines) == 1
