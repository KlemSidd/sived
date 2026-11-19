{{- define "sived.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "sived.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- $name := default .Chart.Name .Values.nameOverride }}
{{- if contains $name .Release.Name }}
{{- .Release.Name | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}
{{- end }}

{{- define "sived.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "sived.labels" -}}
helm.sh/chart: {{ include "sived.chart" . }}
app.kubernetes.io/name: {{ include "sived.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{- define "sived.selectorLabels" -}}
app.kubernetes.io/name: {{ include "sived.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/component: web
{{- end }}

{{- define "sived.serviceAccountName" -}}
{{- if .Values.serviceAccount.create }}
{{- default (include "sived.fullname" .) .Values.serviceAccount.name }}
{{- else }}
{{- default "default" .Values.serviceAccount.name }}
{{- end }}
{{- end }}

{{- define "sived.image" -}}
{{- printf "%s:%s" .Values.image.repository (.Values.image.tag | default .Chart.AppVersion) }}
{{- end }}

{{- define "sived.secretName" -}}
{{- if .Values.externalSecret.enabled -}}
{{- .Values.externalSecret.target.name | default (printf "%s-env" (include "sived.fullname" .)) -}}
{{- else if .Values.secrets.existingSecret -}}
{{- .Values.secrets.existingSecret -}}
{{- else if or .Values.secrets.xaiApiKey .Values.secrets.llmApiKey .Values.secrets.extra -}}
{{- printf "%s-env" (include "sived.fullname" .) -}}
{{- end -}}
{{- end }}

{{- define "sived.trustProxy" -}}
{{- if ne (toString .Values.env.trustProxy) "" -}}
{{- .Values.env.trustProxy -}}
{{- else if or .Values.ingress.enabled .Values.gateway.enabled -}}
1
{{- else -}}
0
{{- end -}}
{{- end }}

{{- define "sived.probeScheme" -}}
{{- if .Values.probes.scheme -}}
{{- .Values.probes.scheme -}}
{{- else if .Values.tls.existingSecret -}}
HTTPS
{{- else -}}
HTTP
{{- end -}}
{{- end }}

{{- define "sived.tlsSecretName" -}}
{{- .Values.certManager.certificate.secretName | default (printf "%s-tls" (include "sived.fullname" .)) -}}
{{- end }}

{{- define "sived.secretEnv" -}}
{{- $name := include "sived.secretName" . -}}
{{- if $name }}
- name: XAI_API_KEY
  valueFrom:
    secretKeyRef:
      name: {{ $name }}
      key: XAI_API_KEY
      optional: true
- name: LLM_API_KEY
  valueFrom:
    secretKeyRef:
      name: {{ $name }}
      key: LLM_API_KEY
      optional: true
{{- range $key, $_ := .Values.secrets.extra }}
- name: {{ $key }}
  valueFrom:
    secretKeyRef:
      name: {{ $name }}
      key: {{ $key }}
      optional: true
{{- end }}
{{- range .Values.secrets.extraKeys }}
- name: {{ . }}
  valueFrom:
    secretKeyRef:
      name: {{ $name }}
      key: {{ . }}
      optional: true
{{- end }}
{{- end }}
{{- end }}

{{- define "sived.env" -}}
- name: HOST
  value: "0.0.0.0"
- name: PORT
  value: "8080"
- name: DATA_DIR
  value: /data
- name: DEV_LOGIN
  value: {{ .Values.env.devLogin | quote }}
- name: DEMO_MODE
  value: {{ .Values.env.demoMode | quote }}
- name: TRUST_PROXY
  value: {{ include "sived.trustProxy" . | quote }}
- name: LLM_PROVIDER
  value: {{ .Values.env.llmProvider | quote }}
- name: XAI_MODE
  value: {{ .Values.env.xaiMode | quote }}
{{- if .Values.env.xaiModel }}
- name: XAI_MODEL
  value: {{ .Values.env.xaiModel | quote }}
{{- end }}
{{- if .Values.env.xaiBaseUrl }}
- name: XAI_BASE_URL
  value: {{ .Values.env.xaiBaseUrl | quote }}
{{- end }}
{{- if .Values.env.llmMode }}
- name: LLM_MODE
  value: {{ .Values.env.llmMode | quote }}
{{- end }}
{{- if .Values.env.llmModel }}
- name: LLM_MODEL
  value: {{ .Values.env.llmModel | quote }}
{{- end }}
{{- if .Values.env.llmBaseUrl }}
- name: LLM_BASE_URL
  value: {{ .Values.env.llmBaseUrl | quote }}
{{- end }}
{{- if .Values.env.llmTimeout }}
- name: LLM_TIMEOUT
  value: {{ .Values.env.llmTimeout | quote }}
{{- end }}
{{- if .Values.env.dailyStopMicros }}
- name: DAILY_STOP_MICROS
  value: {{ .Values.env.dailyStopMicros | quote }}
{{- end }}
{{- if .Values.env.patreonRedirectUri }}
- name: PATREON_REDIRECT_URI
  value: {{ .Values.env.patreonRedirectUri | quote }}
{{- end }}
{{- if .Values.env.acmeDomain }}
- name: ACME_DOMAIN
  value: {{ .Values.env.acmeDomain | quote }}
- name: ACME_EMAIL
  value: {{ .Values.env.acmeEmail | quote }}
- name: ACME_STAGING
  value: {{ .Values.env.acmeStaging | quote }}
- name: ACME_HTTP_PORT
  value: {{ .Values.env.acmeHttpPort | default "0" | quote }}
{{- if .Values.env.acmeDirectory }}
- name: ACME_DIRECTORY
  value: {{ .Values.env.acmeDirectory | quote }}
{{- end }}
{{- end }}
{{- if .Values.tls.existingSecret }}
- name: TLS_CERT
  value: {{ printf "/tls/%s" .Values.tls.certKey | quote }}
- name: TLS_KEY
  value: {{ printf "/tls/%s" .Values.tls.keyKey | quote }}
{{- end }}
{{- include "sived.secretEnv" . }}
{{- with .Values.extraEnv }}
{{ toYaml . }}
{{- end }}
{{- end }}

{{- define "sived.volumeMounts" -}}
- name: data
  mountPath: /data
- name: tmp
  mountPath: /tmp
{{- if .Values.tls.existingSecret }}
- name: tls
  mountPath: /tls
  readOnly: true
{{- end }}
{{- with .Values.extraVolumeMounts }}
{{ toYaml . }}
{{- end }}
{{- end }}

{{- define "sived.volumes" -}}
- name: data
  {{- if .Values.persistence.enabled }}
  persistentVolumeClaim:
    claimName: {{ .Values.persistence.existingClaim | default (include "sived.fullname" .) }}
  {{- else }}
  emptyDir: {}
  {{- end }}
- name: tmp
  emptyDir: {}
{{- if .Values.tls.existingSecret }}
- name: tls
  secret:
    secretName: {{ .Values.tls.existingSecret }}
{{- end }}
{{- with .Values.extraVolumes }}
{{ toYaml . }}
{{- end }}
{{- end }}

{{- define "sived.probeSocket" -}}
{{- if eq (include "sived.probeScheme" .) "TCP" -}}
tcpSocket:
  port: http
{{- else -}}
httpGet:
  path: /healthz
  port: http
  scheme: {{ include "sived.probeScheme" . }}
{{- end }}
{{- end }}

{{- define "sived.workerLoop" -}}
file={{ .Values.worker.databaseFile | quote }}
while [ ! -f "$file" ]; do sleep 1; done
while true; do python -m app work || true; sleep "${WORKER_INTERVAL:-15}"; done
{{- end }}

{{- define "sived.workerOnce" -}}
file={{ .Values.worker.databaseFile | quote }}
i=0
while [ ! -f "$file" ] && [ "$i" -lt 60 ]; do
  sleep 1
  i=$((i+1))
done
if [ ! -f "$file" ]; then
  echo "the data file is not on this volume yet" >&2
  exit 1
fi
python -m app work
{{- end }}
