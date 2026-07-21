{{- define "kubevoice.name" -}}
{{- .Chart.Name -}}
{{- end -}}

{{- define "kubevoice.fullname" -}}
{{- printf "%s" .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "kubevoice.labels" -}}
app.kubernetes.io/name: {{ include "kubevoice.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}

{{- define "kubevoice.serviceAccountName" -}}
{{- if .Values.serviceAccount.create -}}
{{- default "kubevoice" .Values.serviceAccount.name -}}
{{- else -}}
{{- default "default" .Values.serviceAccount.name -}}
{{- end -}}
{{- end -}}
