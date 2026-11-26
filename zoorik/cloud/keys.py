"""The key that names each cloud in what the run reads, and the cloud's name as the run writes it."""

AZURE = "azure"
AWS = "aws"
CLOUD_NAMES = {AZURE: "Azure", AWS: "AWS"}
# Each cloud's Kubernetes service, whose clusters the run assesses.
PLATFORMS = {AZURE: "AKS", AWS: "EKS"}
