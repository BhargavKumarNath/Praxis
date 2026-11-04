terraform {
  required_version = ">= 1.7" # terraform test with mock_provider (make tf-test)

  # State lives in a versioned GCS bucket (docs/deployment.md); the bucket is passed at init:
  #   terraform init -backend-config=bucket=<project>-tfstate
  # make tf-test / tf-check use -backend=false and never touch it.
  backend "gcs" {
    prefix = "envs/dev"
  }

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 6.0"
    }
    google-beta = {
      source  = "hashicorp/google-beta"
      version = "~> 6.0"
    }
  }
}
