-- Run after schema.sql as database administrator. Raises on insecure grants/RLS.
do $$
declare name text; role_name text; permission text;
begin
  foreach name in array array['trades','market_snapshots','model_checkpoints','risk_state','decision_logs'] loop
    if not (select relrowsecurity from pg_class where oid = ('public.'||name)::regclass) then
      raise exception 'RLS disabled: %', name;
    end if;
    foreach role_name in array array['anon','authenticated'] loop
      foreach permission in array array['SELECT','INSERT','UPDATE','DELETE'] loop
        if has_table_privilege(role_name,'public.'||name,permission) then
          raise exception 'Unexpected % privilege for % on %', permission, role_name, name;
        end if;
      end loop;
    end loop;
    if not has_table_privilege('service_role','public.'||name,'INSERT') then
      raise exception 'Service insert grant missing: %', name;
    end if;
  end loop;
end $$;
