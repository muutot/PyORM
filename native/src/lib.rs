#![forbid(unsafe_code)]

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::{
    PyBool, PyBytes, PyDict, PyFloat, PyFrozenSet, PyInt, PyList, PySet, PyString, PyTuple, PyType,
};
use std::collections::{HashMap, HashSet};

fn scalar(value: &Bound<'_, PyAny>) -> bool {
    value.is_none()
        || value.is_exact_instance_of::<PyInt>()
        || value.is_exact_instance_of::<PyString>()
        || value.is_exact_instance_of::<PyFloat>()
        || value.is_exact_instance_of::<PyBool>()
        || value.is_exact_instance_of::<PyBytes>()
}

fn copy_tree<'py>(
    py: Python<'py>,
    value: &Bound<'py, PyAny>,
    memo: &Bound<'py, PyDict>,
    deepcopy: &Bound<'py, PyAny>,
    depth: usize,
) -> PyResult<Bound<'py, PyAny>> {
    if scalar(value) {
        return Ok(value.clone());
    }
    if depth >= 32
        || (!value.is_exact_instance_of::<PyList>() && !value.is_exact_instance_of::<PyDict>())
    {
        return deepcopy.call1((value, memo));
    }
    let identity = value.as_ptr() as usize;
    if let Some(result) = memo.get_item(identity)? {
        return Ok(result);
    }
    let result = if value.is_exact_instance_of::<PyList>() {
        let result = PyList::empty(py);
        memo.set_item(identity, &result)?;
        for item in value.try_iter()? {
            let item = item?;
            result.append(copy_tree(py, &item, memo, deepcopy, depth + 1)?)?;
        }
        result.into_any()
    } else {
        let result = PyDict::new(py);
        memo.set_item(identity, &result)?;
        for pair in value.call_method0("items")?.try_iter()? {
            let pair = pair?;
            let pair = pair.cast::<PyTuple>()?;
            let key = pair.get_item(0)?;
            let item = pair.get_item(1)?;
            // Python evaluates the assignment value before its subscript key.
            let copied_item = copy_tree(py, &item, memo, deepcopy, depth + 1)?;
            let copied_key = copy_tree(py, &key, memo, deepcopy, depth + 1)?;
            result.set_item(copied_key, copied_item)?;
        }
        result.into_any()
    };
    // deepcopy retains originals to prevent id reuse when a custom copy mutates its source.
    let alive_key = memo.as_ptr() as usize;
    if let Some(alive) = memo.get_item(alive_key)? {
        alive.call_method1("append", (value,))?;
    } else {
        memo.set_item(alive_key, PyList::new(py, [value])?)?;
    }
    Ok(result)
}

#[pyfunction]
fn copy_bson<'py>(
    py: Python<'py>,
    value: &Bound<'py, PyAny>,
    memo: &Bound<'py, PyDict>,
    deepcopy: &Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyAny>> {
    copy_tree(py, value, memo, deepcopy, 0)
}

fn field_key<'py>(
    key: &Bound<'py, PyAny>,
    fallback: &Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyAny>> {
    let text = if key.is_exact_instance_of::<PyInt>() {
        key.str()?.into_any()
    } else if key.is_exact_instance_of::<PyString>() {
        key.clone()
    } else {
        return fallback.call1((key,));
    };
    if let Ok(value) = text.cast::<PyString>()?.to_str() {
        if !value.is_empty() && !value.contains(['.', '$', '\0']) {
            return Ok(text);
        }
    }
    fallback.call1((key,))
}

fn tracked_dict(value: &Bound<'_, PyAny>, tracked: &Bound<'_, PyType>) -> bool {
    value.is_exact_instance_of::<PyDict>() || value.get_type().is(tracked)
}

fn tracked_list(value: &Bound<'_, PyAny>, tracked: &Bound<'_, PyType>) -> bool {
    value.is_exact_instance_of::<PyList>() || value.get_type().is(tracked)
}

struct Encoder<'a, 'py> {
    fallback: &'a Bound<'py, PyAny>,
    key: &'a Bound<'py, PyAny>,
    dict: &'a Bound<'py, PyType>,
    list: &'a Bound<'py, PyType>,
}

impl<'py> Encoder<'_, 'py> {
    fn walk(&self, value: &Bound<'py, PyAny>, depth: usize) -> PyResult<Bound<'py, PyAny>> {
        if scalar(value) {
            return Ok(value.clone());
        }
        if depth >= 32 {
            return self.fallback.call1((value,));
        }
        if value.hasattr("to_mongo")? {
            return self.fallback.call1((value,));
        }
        let py = value.py();
        if tracked_dict(value, self.dict) {
            let result = PyDict::new(py);
            for pair in value.call_method0("items")?.try_iter()? {
                let pair = pair?;
                let pair = pair.cast::<PyTuple>()?;
                let key = pair.get_item(0)?;
                let item = pair.get_item(1)?;
                let key = field_key(&key, self.key)?;
                if result.contains(&key)? {
                    // Keep the original exception type and repr formatting.
                    let message = format!(
                        "Dictionary keys collide after BSON conversion: {}",
                        key.repr()?
                    );
                    let error = py
                        .import("pyorm.exceptions")?
                        .getattr("ValidationError")?
                        .call1((message,))?;
                    return Err(PyErr::from_value(error));
                }
                result.set_item(key, self.walk(&item, depth + 1)?)?;
            }
            return Ok(result.into_any());
        }
        if tracked_list(value, self.list) || value.is_exact_instance_of::<PyTuple>() {
            let result = PyList::empty(py);
            if value.is_exact_instance_of::<PyTuple>() {
                for item in value.cast::<PyTuple>()?.iter() {
                    result.append(self.walk(&item, depth + 1)?)?;
                }
            } else {
                for item in value.try_iter()? {
                    let item = item?;
                    result.append(self.walk(&item, depth + 1)?)?;
                }
            }
            return Ok(result.into_any());
        }
        self.fallback.call1((value,))
    }
}

#[pyfunction]
fn encode_value<'py>(
    value: &Bound<'py, PyAny>,
    fallback: &Bound<'py, PyAny>,
    key: &Bound<'py, PyAny>,
    dict_type: &Bound<'py, PyType>,
    list_type: &Bound<'py, PyType>,
) -> PyResult<Bound<'py, PyAny>> {
    Encoder {
        fallback,
        key,
        dict: dict_type,
        list: list_type,
    }
    .walk(value, 0)
}

fn collect_paths<'py>(
    value: &Bound<'py, PyAny>,
    path: &Bound<'py, PyString>,
    encoder: &Encoder<'_, 'py>,
    result: &Bound<'py, PySet>,
    depth: usize,
) -> PyResult<()> {
    if scalar(value) {
        return Ok(());
    }
    if depth >= 32 {
        for item in encoder.fallback.call1((value, path))?.try_iter()? {
            result.add(item?)?;
        }
        return Ok(());
    }
    if value.is_instance_of::<PySet>() || value.is_instance_of::<PyFrozenSet>() {
        return result.add(path);
    }
    if tracked_dict(value, encoder.dict) {
        for pair in value.call_method0("items")?.try_iter()? {
            let pair = pair?;
            let pair = pair.cast::<PyTuple>()?;
            let key = pair.get_item(0)?;
            let item = pair.get_item(1)?;
            let key = field_key(&key, encoder.key)?;
            if scalar(&item) {
                continue;
            }
            let prefix = path.to_str();
            let suffix = key.cast::<PyString>()?.to_str();
            if let (Ok(prefix), Ok(suffix)) = (prefix, suffix) {
                let child = PyString::new(value.py(), &format!("{prefix}.{suffix}"));
                collect_paths(&item, &child, encoder, result, depth + 1)?;
            } else {
                // Python strings may contain lone surrogates. Concatenate in Python
                // rather than revisiting the parent and repeating custom callbacks.
                let child = path
                    .call_method1("__add__", (".",))?
                    .call_method1("__add__", (key,))?;
                collect_paths(&item, child.cast::<PyString>()?, encoder, result, depth + 1)?;
            }
        }
    } else if (!tracked_list(value, encoder.list) && !value.is_exact_instance_of::<PyTuple>())
        || value.hasattr("_fields")?
    {
        for found in encoder.fallback.call1((value, path))?.try_iter()? {
            result.add(found?)?;
        }
    }
    Ok(())
}

#[pyfunction]
fn collect_set_paths<'py>(
    value: &Bound<'py, PyAny>,
    path: &Bound<'py, PyString>,
    fallback: &Bound<'py, PyAny>,
    key: &Bound<'py, PyAny>,
    dict_type: &Bound<'py, PyType>,
    list_type: &Bound<'py, PyType>,
) -> PyResult<Bound<'py, PySet>> {
    let result = PySet::empty(value.py())?;
    let encoder = Encoder {
        fallback,
        key,
        dict: dict_type,
        list: list_type,
    };
    collect_paths(value, path, &encoder, &result, 0)?;
    Ok(result)
}

#[derive(Clone, Copy, Debug, Hash, Eq, PartialEq)]
struct ValueKey<'a> {
    kind: u8,
    payload: &'a [u8],
}

fn length(data: &[u8], offset: usize) -> Result<usize, &'static str> {
    let bytes: [u8; 4] = data
        .get(offset..offset.checked_add(4).ok_or("BSON length overflow")?)
        .ok_or("Truncated BSON length")?
        .try_into()
        .map_err(|_| "Truncated BSON length")?;
    let value = i32::from_le_bytes(bytes);
    usize::try_from(value).map_err(|_| "Negative BSON length")
}

fn cstring_end(data: &[u8], offset: usize) -> Result<usize, &'static str> {
    let tail = data.get(offset..).ok_or("Truncated BSON cstring")?;
    let end = tail
        .iter()
        .position(|byte| *byte == 0)
        .ok_or("Unterminated BSON cstring")?;
    offset.checked_add(end + 1).ok_or("BSON cstring overflow")
}

fn array_body(data: &[u8]) -> Result<&[u8], &'static str> {
    if data.len() < 13
        || length(data, 0)? != data.len()
        || data.get(4..7) != Some(&[4, b'v', 0])
        || length(data, 7)? != data.len() - 8
        || data[data.len() - 1] != 0
        || data[data.len() - 2] != 0
    {
        return Err("Expected a BSON document containing only array 'v'");
    }
    Ok(&data[11..data.len() - 2])
}

fn keys(data: &[u8]) -> Result<Vec<ValueKey<'_>>, &'static str> {
    let body = array_body(data)?;
    let mut result = Vec::new();
    let mut offset = 0;
    while offset < body.len() {
        let kind = body[offset];
        offset = cstring_end(body, offset + 1)?;
        let size = match kind {
            0x01 | 0x09 | 0x11 | 0x12 => 8,
            0x02 | 0x0d | 0x0e => {
                let size = length(body, offset)?;
                if size < 1 {
                    return Err("Invalid BSON string length");
                }
                size.checked_add(4).ok_or("BSON string overflow")?
            }
            0x03 | 0x04 | 0x0f => {
                let size = length(body, offset)?;
                if size < 5 {
                    return Err("Invalid BSON document length");
                }
                size
            }
            0x05 => length(body, offset)?
                .checked_add(5)
                .ok_or("BSON binary overflow")?,
            0x06 | 0x0a | 0x7f | 0xff => 0,
            0x07 => 12,
            0x08 => 1,
            0x0b => cstring_end(body, cstring_end(body, offset)?)? - offset,
            0x0c => length(body, offset)?
                .checked_add(16)
                .ok_or("BSON DBPointer overflow")?,
            0x10 => 4,
            0x13 => 16,
            _ => return Err("Unknown BSON value type"),
        };
        let end = offset.checked_add(size).ok_or("BSON value overflow")?;
        let payload = body.get(offset..end).ok_or("Truncated BSON value")?;
        result.push(ValueKey { kind, payload });
        offset = end;
    }
    Ok(result)
}

fn set_order(data: &[u8]) -> Result<Vec<usize>, &'static str> {
    let values = keys(data)?;
    let mut indices: Vec<usize> = (0..values.len()).collect();
    indices.sort_by(|left, right| {
        let a = values[*left];
        let b = values[*right];
        // Python sorts BSON.encode({'v': value}), whose length prefix precedes its type.
        let a_length = ((a.payload.len() + 8) as u32).to_le_bytes();
        let b_length = ((b.payload.len() + 8) as u32).to_le_bytes();
        a_length
            .cmp(&b_length)
            .then_with(|| a.kind.cmp(&b.kind))
            .then_with(|| a.payload.cmp(b.payload))
    });
    Ok(indices)
}

#[pyfunction]
fn sort_set<'py>(
    py: Python<'py>,
    data: &Bound<'py, PyBytes>,
    values: &Bound<'py, PyList>,
) -> PyResult<Bound<'py, PyList>> {
    let bytes = data.as_bytes();
    let order = py
        .detach(|| set_order(bytes))
        .map_err(PyValueError::new_err)?;
    if order.len() != values.len() {
        return Err(PyValueError::new_err(
            "BSON array and Python values differ in length",
        ));
    }
    let result = PyList::empty(py);
    for index in order {
        result.append(values.get_item(index)?)?;
    }
    Ok(result)
}

fn added_indices(old: &[u8], new: &[u8]) -> Result<Option<Vec<usize>>, &'static str> {
    let old_values = keys(old)?;
    let old_members: HashSet<_> = old_values.iter().copied().collect();
    if old_members.len() != old_values.len() {
        return Ok(None);
    }
    let new_values = keys(new)?;
    let mut members = HashMap::new();
    let mut order = Vec::new();
    for (index, key) in new_values.iter().copied().enumerate() {
        if members.insert(key, index).is_none() {
            order.push(key);
        }
    }
    if old_members.iter().any(|key| !members.contains_key(key)) {
        return Ok(None);
    }
    Ok(Some(
        order
            .into_iter()
            .filter(|key| !old_members.contains(key))
            .map(|key| members[&key])
            .collect(),
    ))
}

#[pyfunction]
fn set_added(
    py: Python<'_>,
    old: &Bound<'_, PyBytes>,
    new: &Bound<'_, PyBytes>,
) -> PyResult<Option<Vec<usize>>> {
    let old_bytes = old.as_bytes();
    let new_bytes = new.as_bytes();
    py.detach(|| added_indices(old_bytes, new_bytes))
        .map_err(PyValueError::new_err)
}

#[pyfunction]
fn array_prefix(
    py: Python<'_>,
    old: &Bound<'_, PyBytes>,
    new: &Bound<'_, PyBytes>,
) -> PyResult<bool> {
    let old_bytes = old.as_bytes();
    let new_bytes = new.as_bytes();
    py.detach(|| {
        let before = array_body(old_bytes)?;
        let after = array_body(new_bytes)?;
        Ok(after.starts_with(before))
    })
    .map_err(|error: &'static str| PyValueError::new_err(error))
}

#[pymodule]
fn _native(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_function(wrap_pyfunction!(copy_bson, module)?)?;
    module.add_function(wrap_pyfunction!(encode_value, module)?)?;
    module.add_function(wrap_pyfunction!(collect_set_paths, module)?)?;
    module.add_function(wrap_pyfunction!(sort_set, module)?)?;
    module.add_function(wrap_pyfunction!(set_added, module)?)?;
    module.add_function(wrap_pyfunction!(array_prefix, module)?)?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn malformed_buffers_never_panic() {
        let mut state = 1u64;
        for size in 0..512 {
            let mut bytes = vec![0; size];
            for byte in &mut bytes {
                state = state.wrapping_mul(6364136223846793005).wrapping_add(1);
                *byte = (state >> 32) as u8;
            }
            let _ = keys(&bytes);
            let _ = set_order(&bytes);
            let _ = added_indices(&bytes, &bytes);
        }
    }

    #[test]
    fn empty_arrays_and_truncation_are_checked() {
        let empty = [13, 0, 0, 0, 4, b'v', 0, 5, 0, 0, 0, 0, 0];
        assert!(keys(&empty).unwrap().is_empty());
        assert!(set_order(&empty).unwrap().is_empty());
        for end in 0..empty.len() {
            assert!(keys(&empty[..end]).is_err());
        }
    }
}
