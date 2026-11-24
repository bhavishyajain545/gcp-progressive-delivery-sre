project_id       = "bj-progressive-delivery"
region           = "us-central1"
zone             = "us-central1-a"
authorized_cidrs = ["157.49.0.0/16", "152.58.0.0/16", "182.71.0.0/16", "111.93.0.0/16"] # ISP CGNAT pools + the office's two uplinks (requests alternate between them)
github_repo      = "bhavishyajain545/gcp-progressive-delivery-sre"
alert_email      = "jainbhavishya43@gmail.com"
