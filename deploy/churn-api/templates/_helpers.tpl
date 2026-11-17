{{- define "churn-api.fullname" -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "churn-api.labels" -}}
app.kubernetes.io/name: churn-api
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Values.image.tag | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ .Chart.Name }}-{{ .Chart.Version }}
churn.model/version: {{ .Values.model.version | quote }}
{{- end -}}

{{- define "churn-api.selectorLabels" -}}
app.kubernetes.io/name: churn-api
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{/* A git SHA (7-40 hex chars), never empty or "latest". */}}
{{- define "churn-api.image" -}}
{{- $tag := .Values.image.tag | toString -}}
{{- if not (regexMatch "^[0-9a-f]{7,40}$" $tag) -}}
{{- fail (printf "image.tag must be a git SHA (got %q); never empty or latest" $tag) -}}
{{- end -}}
{{ .Values.image.repository }}:{{ $tag }}
{{- end -}}

{{- define "churn-api.modelUri" -}}
{{- $version := .Values.model.version | toString -}}
{{- if not (regexMatch "^[0-9]+$" $version) -}}
{{- fail (printf "model.version must be a registry version number (got %q)" $version) -}}
{{- end -}}
models:/{{ .Values.model.name }}/{{ $version }}
{{- end -}}
